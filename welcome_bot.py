# -*- coding: utf-8 -*-
"""welcome_bot.py — 新人入群欢迎（@新人 + 大肥鱼口吻现写欢迎语）

功能：轮询 NapCat 拉取所有群成员名单，与上次快照对比；发现新成员就让免费池 LLM
以大肥鱼口吻现写一句随机欢迎语（temperature 0.95，想说啥说啥），@新人发进群。
LLM 失败回退内置欢迎池随机选一条。首次见到某个群只记基线不欢迎（防历史存量误报）。
发送失败按人计次重试，2 次仍失败放弃并记日志（闭环，不无限重试）。

计划任务：MaiBotWelcomeSender 每 2 分钟（py welcome_bot.py）
用法：
  py welcome_bot.py             # 常规轮询（计划任务调用）
  py welcome_bot.py --dry-run   # 全流程但只打印不发消息、不落盘快照（测试用）

网络边界（botcommon 白名单）：127.0.0.1(http, NapCat :3002 读群名单/发消息)
                            / open.bigmodel.cn(https, 智谱免费写欢迎语)
"""
import json
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "welcome_bot_state.json"
LOG_FILE = BASE / "welcome_bot.log"

SELF_QQ = "3879824374"   # 大肥鱼自己，自己进群不自我欢迎
MAX_ATS = 10             # 单条消息最多 @ 几个新人（超出部分正文带过）
MAX_ATTEMPTS = 2         # 发送失败重试上限（按人计次），超过放弃

ALLOWED_LLM = {"open.bigmodel.cn": {"https"}}

FALLBACK = [
    "哦豁，新面孔！随便坐别客气~",
    "来了来了，自己人，别拘束",
    "新朋友！冒个泡让我看看你",
    "欢迎上船，这条鱼带你混",
    "嘿，来得正好，群里最近有点冷清",
    "游进深蓝沟了？别怕，这片海挺好玩",
    "欢迎！群里什么怪人都有，很快你就习惯了",
    "新来的？报上名来——开玩笑的，随便聊",
    "欢迎欢迎，坐我尾巴上看戏也行",
    "来啦？先自报家门，然后就可以开摆了",
]


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"groups": {}, "fail": {}, "welcomed": []}


def save_state(st):
    botcommon.write_json_atomic(STATE_FILE, st, indent=1)


def napcat(path, payload):
    res = botcommon.http_json(
        "http://127.0.0.1:3002" + path, {"127.0.0.1": {"http"}},
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, timeout=30,
    )
    if res.get("status") != "ok":
        raise RuntimeError("NapCat %s 失败: %s" % (path, json.dumps(res, ensure_ascii=False)[:150]))
    return res.get("data") or []


def fetch_members():
    """拉全部群 → {gid: {"name": 群名, "members": {uid_str: 昵称}}}"""
    snap = {}
    for g in napcat("/get_group_list", {}):
        gid = str(g["group_id"])
        members = {}
        for m in napcat("/get_group_member_list", {"group_id": g["group_id"]}):
            uid = str(m["user_id"])
            members[uid] = (m.get("card") or m.get("nickname") or uid).strip() or uid
        snap[gid] = {"name": g.get("group_name", ""), "members": members}
    return snap


PROMPT = """你是大肥鱼，一条鲸鱼娘，群里的活宝吉祥物。
群里刚来了新成员：%s（群：%s）。现在时间是%s。
用你的口吻写一句发在群里的欢迎语，@由系统负责加，你只写正文。
像熟人随口打个招呼，15~40字，想说什么说什么：调侃、玩梗、好奇问一句、随口聊聊都行。
%s
禁止"欢迎加入大家庭""祝你玩得开心"这类客套话，禁止堆emoji（最多1个），不提感情分、钱、收款。
只输出这句话本身，不要引号不要解释。"""


def gen_welcome(gname, members):
    if len(members) == 1:
        who = members[0][1]
    else:
        names = "、".join(n for _, n in members[:3])
        who = "%s（共%d位）" % (names, len(members))
    now = datetime.now().strftime("%H:%M")
    hour_tip = ""
    if now < "07:00":
        hour_tip = "这么晚/这么早进群的，可以顺势调侃一句。"
    body = json.dumps({
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": PROMPT % (who, gname, now, hour_tip)}],
        "temperature": 0.95,
    }).encode("utf-8")
    try:
        res = botcommon.http_json(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions", ALLOWED_LLM,
            data=body, headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + botcommon.load_provider_key("Zhipu"),
            }, timeout=60,
        )
        text = res["choices"][0]["message"]["content"].strip()
        text = text.removeprefix("```").removesuffix("```").strip()
        for q in ('"', "“", "”"):
            text = text.strip(q)
        text = text.replace("\n", " ").strip()
        if text and len(text) <= 80:
            return text
        log("LLM 输出异常，回退欢迎池: %r" % text[:60])
    except Exception as e:
        log("LLM 失败回退欢迎池: %r" % e)
    return secrets.choice(FALLBACK)


def main():
    dry = "--dry-run" in sys.argv
    st = load_state()
    try:
        snap = fetch_members()
    except Exception as e:
        log("ERROR: NapCat 拉群信息失败，本轮放弃下轮重试: %r" % e)
        raise SystemExit(1)

    # 找新成员（首次见到的群只记基线，不欢迎）
    news = {}
    for gid, info in snap.items():
        if gid not in st["groups"]:
            st["groups"][gid] = {"name": info["name"], "members": sorted(info["members"])}
            log("init baseline: 群%s(%s) %d人" % (info["name"], gid, len(info["members"])))
            continue
        known = set(st["groups"][gid]["members"])
        fresh = [u for u in sorted(info["members"]) if u not in known and u != SELF_QQ]
        if fresh:
            news[gid] = [(u, info["members"][u]) for u in fresh]

    # 无新人群：刷新基线（顺带处理退群成员，重新进群会再欢迎）
    for gid, info in snap.items():
        if gid in st["groups"] and gid not in news:
            st["groups"][gid] = {"name": info["name"], "members": sorted(info["members"])}

    for gid, members in news.items():
        gname = snap[gid]["name"]
        text = gen_welcome(gname, members)
        ats = "".join("[CQ:at,qq=%s] " % u for u, _ in members[:MAX_ATS])
        msg = ats + text
        time.sleep(secrets.randbelow(12) + 2)  # 拟人抖动，别掐着秒回
        sent = False
        if dry:
            print("[DRYRUN] 群%s(%s) -> %s" % (gname, gid, msg))
            sent = True
        else:
            try:
                napcat("/send_group_msg", {"group_id": int(gid), "message": msg})
                sent = True
            except Exception as e:
                log("发送失败 群%s(%s): %r" % (gname, gid, e))
        fails = st["fail"].setdefault(gid, {})
        if sent and not dry:
            st["groups"][gid]["members"] = sorted(snap[gid]["members"])
            for u, _ in members:
                fails.pop(u, None)
            entry = "%s 群%s @%s %s" % (
                datetime.now().strftime("%m-%d %H:%M"), gname,
                ",".join(u for u, _ in members), text)
            st["welcomed"] = [entry] + st.get("welcomed", [])
            st["welcomed"] = st["welcomed"][:20]
            log("SENT 群%s(%s): @%s %s" % (gname, gid, ",".join(u for u, _ in members), text))
        else:
            # 失败重试闭环：计次，超过上限的放弃（写进基线不再重试）
            give_up = []
            keep_retry = []
            for u, _ in members:
                fails[u] = fails.get(u, 0) + 1
                if fails[u] >= MAX_ATTEMPTS:
                    give_up.append(u)
                else:
                    keep_retry.append(u)
            known = set(st["groups"][gid]["members"])
            st["groups"][gid]["members"] = sorted((known | set(snap[gid]["members"])) - set(keep_retry))
            if give_up:
                log("放弃欢迎 群%s: %s（重试%d次仍失败）" % (gname, ",".join(give_up), MAX_ATTEMPTS))

    if not dry:
        save_state(st)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log("ERROR: %r" % e)
        raise SystemExit(1)
