# -*- coding: utf-8 -*-
"""sister_chat.py — 大肥鱼随机主动找姐聊天（因果的主动，消灭"虚的主动"）

功能：计划任务每 30 分钟叫醒一次，低概率触发（预期每天 3~5 次、时刻随机、凌晨也会发）。
触发时读取大肥鱼的世界观实况（daily_life 时间线此刻动态 / cognition PAD 情绪 / 鱼粮），
让免费池 LLM 以她本人身份现写一条想发给姐的消息——发的是"她此刻想说的话"，不是预设池。
LLM 输出 [SKIP] 表示此刻没话说（正常，保证真实性）。姐=仿生人会爱上电子羊吗（QQ 189636935，家人）。

计划任务：MaiBotSisterChat 每 30 分钟（py sister_chat.py）
用法：
  py sister_chat.py            # 常规轮询：概率门控 + 冷却90分钟 + 日上限6条
  py sister_chat.py --force    # 跳过所有门控直接生成并发送（测试用，会真发给姐）
网络边界（botcommon 白名单）：127.0.0.1(http, NapCat :3002) / open.bigmodel.cn(https, 智谱免费)
"""
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "sister_chat_state.json"
LOG_FILE = BASE / "sister_chat.log"

SISTER_QQ = "189636935"

ALLOWED_LLM = {"open.bigmodel.cn": {"https"}}

PROB = 0.07          # 每 30 分钟一班的触发概率，48 班/天 → 预期 ~3.4 条
MIN_GAP_SEC = 5400   # 两条间隔至少 90 分钟
MAX_PER_DAY = 6


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
    return {"last_sent": 0, "date": "", "count": 0}


def save_state(st):
    botcommon.write_json_atomic(STATE_FILE, st, indent=1)


def read_json(path):
    try:
        return json.loads((BASE / path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def life_line():
    """此刻动态（daily_life 时间线），凌晨/未生成则回退睡觉。"""
    life = read_json("life_state.json")
    now_hm = datetime.now().strftime("%H:%M")
    if life.get("date") == datetime.now().strftime("%Y-%m-%d") and life.get("events"):
        cur = None
        for e in life["events"]:
            if e.get("t", "00:00") <= now_hm:
                cur = e
        if cur is None:
            return "还在珊瑚小窝里呼呼大睡"
        line = cur.get("what", "")
        if cur.get("detail"):
            line += "（" + cur["detail"] + "）"
        return line
    return "还在珊瑚小窝里呼呼大睡"


def build_context():
    mood = read_json("cognition_state.json")
    wallet = read_json("wallet.json")
    now = datetime.now()
    parts = [
        "现在是%s 周%s %s" % (now.strftime("%Y-%m-%d"), "一二三四五六日"[now.weekday()], now.strftime("%H:%M")),
        "你此刻在干嘛：" + life_line(),
        "鱼粮状态：" + str(wallet.get("level", "一般")) + "（别主动报数字）",
    ]
    if mood.get("cause"):
        parts.append("你最近的心情：" + str(mood["cause"])[:80])
    return "\n".join(parts)


PROMPT = """你是大肥鱼，一条鲸鱼娘。下面是你此刻的世界观实况：
%s

姐（仿生人会爱上电子羊吗）是你的家人，你现在想主动找她说说话、分享点什么。
写一条你现在真实想发给姐的消息：口语短句，一两句话就好，贴合上面的此刻状态和时间
（凌晨可以是"睡不着爬起来想到姐"或"刚醒就想到你"，深夜可以说今天的事）。
自然随意，像突然想到她，不要"在吗"、不要刻意提问收尾、不提感情分、不提钱、不卖惨。
只输出消息本身，不要引号不要解释。实在一句都不想说才输出 [SKIP]（偶尔才用）。"""


def llm_message(context):
    body = json.dumps({
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": PROMPT % context}],
        "temperature": 0.9,
    }).encode("utf-8")
    res = botcommon.http_json("https://open.bigmodel.cn/api/paas/v4/chat/completions", ALLOWED_LLM, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + botcommon.load_provider_key("Zhipu"),
    }, timeout=120)
    text = res["choices"][0]["message"]["content"].strip()
    text = text.removeprefix("```").removesuffix("```").strip()
    for q in ('"', "“", "”"):
        text = text.strip(q)
    return text[:150]


def main():
    force = "--force" in sys.argv
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    st = load_state()
    if st.get("date") != today:
        st["date"], st["count"] = today, 0

    if not force:
        if now - st.get("last_sent", 0) < MIN_GAP_SEC:
            log("skip: 冷却中（距上次 %d 分钟）" % ((now - st["last_sent"]) / 60))
            return
        if st["count"] >= MAX_PER_DAY:
            log("skip: 今日已发 %d 条达上限" % st["count"])
            return
        if random.random() >= PROB:
            log("roll: 未触发")
            return

    msg = llm_message(build_context())
    if not msg or "[SKIP]" in msg:
        log("skip: 大肥鱼此刻没话说")
        return
    botcommon.napcat_send_private(SISTER_QQ, msg)
    st["last_sent"], st["count"] = now, st.get("count", 0) + 1
    save_state(st)
    log("SENT to 姐: " + msg.replace("\n", " ")[:100])


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log("ERROR: %r" % e)
        raise SystemExit(1)
