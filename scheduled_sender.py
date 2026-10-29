# -*- coding: utf-8 -*-
"""scheduled_sender.py — 定时消息发送器

按 schedule_config.json 的时间表，通过本机 NapCat HTTP API 给特定人/群发消息。
由计划任务 MaiBotScheduledSender 每分钟调用一次，匹配当前 HH:MM 的启用任务即发送，
state 文件记录当天已发记录防止重复。

用法：
    py scheduled_sender.py                # 常规运行（计划任务调用）
    py scheduled_sender.py --test 早安问候  # 忽略时间表，立即发指定任务（端到端测试）

配置：同目录 schedule_config.json；任务可设 life_hook=true，优先用当日 life_state 实况（WaveB）。times 用 24 小时制 HH:MM；days 缺省每天，
可写 [1,2,3,4,5] 表示周一到周五（0=周日）；messages 是消息池，每次随机选一条。
网络边界：仅允许本机 NapCat 127.0.0.1:3002，禁止重定向（见 botcommon.py）。
"""
import json
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "schedule_config.json"
STATE_FILE = BASE / "scheduled_sender_state.json"

NAPCAT_PRIVATE = "http://127.0.0.1:3002/send_private_msg"
NAPCAT_GROUP = "http://127.0.0.1:3002/send_group_msg"


def life_hook_message():
    """从当日 life_state.json 拼一条短实况；失败返回 None，调用方回退消息池。"""
    try:
        life = json.loads((BASE / "life_state.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    today = time.strftime("%Y-%m-%d")
    if life.get("date") != today:
        return None
    now_hm = time.strftime("%H:%M")
    cur = None
    for e in life.get("events") or []:
        if e.get("t", "00:00") <= now_hm:
            cur = e
    if cur is None and life.get("events"):
        first = life["events"][0]
        what = first.get("what", "刚醒")
        return "刚醒呢……等下要" + what + "啦"
    if not cur:
        return None
    what = (cur.get("what") or "").strip()
    detail = (cur.get("detail") or "").strip()
    if not what:
        return None
    # 口语短句，避免像日报
    if detail:
        return what + "，" + detail[:28]
    return what



def send_one(task, text):
    if task["target"]["type"] == "private":
        res = botcommon.http_json(
            NAPCAT_PRIVATE, {"127.0.0.1": {"http"}},
            data=json.dumps({"user_id": task["target"]["id"], "message": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    else:
        res = botcommon.http_json(
            NAPCAT_GROUP, {"127.0.0.1": {"http"}},
            data=json.dumps({"group_id": task["target"]["id"], "message": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    if res.get("status") != "ok":
        raise RuntimeError("NapCat 发送失败: " + json.dumps(res, ensure_ascii=False)[:200])


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"sent": []}


def save_state(st):
    botcommon.write_json_atomic(STATE_FILE, st)


def main():
    test_name = None
    args = sys.argv[1:]
    if "--test" in args:
        idx = args.index("--test")
        if idx + 1 >= len(args):
            raise SystemExit("用法: py scheduled_sender.py --test 任务名")
        test_name = args[idx + 1]

    if not CONFIG_FILE.exists():
        raise SystemExit("缺少配置文件: " + str(CONFIG_FILE))
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    now_hm = time.strftime("%H:%M")
    today = time.strftime("%Y-%m-%d")
    weekday = int(time.strftime("%w"))  # 0=周日, 1-6=周一到周六
    st = load_state()
    # 只清当天的周期任务记录；once 开头的一次性任务完成记录要永久保留（防止重发+供查询显示状态）
    st["sent"] = [k for k in st["sent"] if k.startswith(today) or k.startswith("once ")]

    matched = False
    now_dt = datetime.now()
    for task in cfg.get("tasks", []):
        name = task.get("name", "未命名")
        if test_name is not None:
            if name != test_name or not task.get("enabled", False):
                continue
            matched = True
        else:
            if not task.get("enabled", False):
                continue
            once = str(task.get("once") or "").strip()
            if once:
                # 一次性任务：到点发送；超过 2 小时未发（如关机错过）标记完成不再补发
                key = "once " + once + " " + name
                if key in st["sent"]:
                    continue
                try:
                    due = datetime.strptime(once, "%Y-%m-%d %H:%M")
                except ValueError:
                    print("任务 [" + name + "] once 格式非法:", once)
                    continue
                if now_dt < due:
                    continue
                matched = True
                if (now_dt - due).total_seconds() > 7200:
                    st["sent"].append(key)
                    print("过期跳过 [" + name + "] once=" + once)
                    continue
            else:
                if now_hm not in task.get("times", []):
                    continue
                days = task.get("days")
                if days and weekday not in days:
                    continue
                key = today + " " + now_hm + " " + name
                if key in st["sent"]:
                    continue
                matched = True

        text = None
        if task.get("life_hook"):
            text = life_hook_message()
            if text:
                print("任务 [" + name + "] 用时间线实况")
        if not text:
            msgs = task.get("messages", [])
            if isinstance(msgs, str):
                msgs = [msgs]
            if not msgs:
                print("任务 [" + name + "] 没有可发消息，跳过")
                continue
            text = secrets.choice(msgs)
        try:
            send_one(task, text)
        except Exception as e:
            print("发送失败 [" + name + "]:", e)
            continue
        print("已发送 [" + name + "]:", text[:40])
        if test_name is None:
            # once 分支的 key 是 "once <时间> <任务名>"，周期分支是 "日期 HH:MM 任务名"，
            # 必须记各自分支构造的 key，否则 once 任务会因查不到记录而每分钟重发
            st["sent"].append(key)

    if test_name is not None and not matched:
        print("未找到名为 [" + test_name + "] 且启用的任务")
    save_state(st)


if __name__ == "__main__":
    main()
