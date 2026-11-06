# -*- coding: utf-8 -*-
"""sister_chat.py — 大肥鱼随机主动找姐聊天（总开关默认关）

功能：计划任务每 30 分钟叫醒一次。总开关 sister_chat_config.json 的 enabled
为 false（或缺文件、坏 JSON、无法识别的值）时，本进程在概率门控、LLM、NapCat
之前直接返回，不主动私聊。enabled 为 true 时才走原门控（概率 + 冷却 90 分钟
+ 日上限 6 条），用免费池 LLM 按世界观实况现写一条。--force 只跳过概率/冷却/日上限，
不绕过总开关。

计划任务：MaiBotSisterChat。deploy/crontab.maibot 里该行已注释；
即使旧 crontab 仍每 30 分钟调用本脚本，enabled=false 也不会生成或发送。
同一开关也让 scheduled_sender 跳过发给姐的私聊任务；群任务和其他私聊不拦。
姐发来的消息不经过本脚本，仍由 MaiBot 正常回复。

用法：
  py sister_chat.py            # 常规轮询；关闭时只记一行 skip
  py sister_chat.py --probe    # 打印开关 JSON，不生成不发送
  py sister_chat.py --force    # 仅当 enabled=true 时跳过门控并发送（会真发）
网络边界：关闭或 --probe 时不出网。开启时 127.0.0.1(http, NapCat :3002) / 免费池 LLM（默认不烧 DeepSeek）。
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
CONFIG_FILE = BASE / "sister_chat_config.json"

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


def _enabled_flag(value):
    """只认明确的开/关。无法识别时关闭，避免字符串 "false" 被当成真。"""
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "on", "1"):
            return True
        if v in ("false", "no", "off", "0"):
            return False
    return False


def load_config(path=None):
    """读总开关。缺文件、坏 JSON、非对象、缺 enabled 一律关闭。"""
    path = Path(path) if path else CONFIG_FILE
    if not path.is_file():
        return {"enabled": False, "source": "missing"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": False, "source": "invalid"}
    if not isinstance(raw, dict):
        return {"enabled": False, "source": "invalid"}
    return {"enabled": _enabled_flag(raw.get("enabled", False)), "source": "file"}


def proactive_allowed(cfg):
    """总开关。False 时不调用 LLM、不调用 NapCat。"""
    return bool((cfg or {}).get("enabled", False))


def probe_status(cfg=None):
    """开关探测。不生成消息、不发送。"""
    cfg = load_config() if cfg is None else cfg
    allowed = proactive_allowed(cfg)
    return {
        "config_file": CONFIG_FILE.name,
        "enabled": allowed,
        "config_source": cfg.get("source", "given"),
        "proactive_private_send": "on" if allowed else "off",
        "force_overrides_switch": False,
        "blocks_before_llm": not allowed,
        "blocks_before_napcat": not allowed,
    }


def load_state(path=None):
    path = Path(path) if path else STATE_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"last_sent": 0, "date": "", "count": 0}


def save_state(st, path=None):
    botcommon.write_json_atomic(Path(path) if path else STATE_FILE, st, indent=1)


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
    """商汤→智谱免费优先；失败本轮跳过（不烧 DeepSeek）。"""
    text, provider, model = botcommon.llm_free_chat(PROMPT % context, temperature=0.9, timeout=120, allow_paid=False)
    text = text.removeprefix("```").removesuffix("```").strip()
    for q in ('"', "“", "”"):
        text = text.strip(q)
    if provider != "Zhipu":
        log("llm 通道: %s %s" % (provider, model))
    return text[:150]


def main(argv=None, send_private=None, llm_fn=None, config_path=None, state_path=None, roll=None, now_fn=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = load_config(config_path)
    if "--probe" in argv:
        status = probe_status(cfg)
        print(json.dumps(status, ensure_ascii=False))
        return status

    # 总开关在门控、读状态、LLM、发送之前。--force 不能绕过。
    if not proactive_allowed(cfg):
        log("skip: 总开关关闭（sister_chat_config.json enabled=false），不生成、不发送")
        return {"action": "skip", "reason": "disabled", "sent": False}

    force = "--force" in argv
    now = (now_fn or time.time)()
    today = datetime.now().strftime("%Y-%m-%d")
    st = load_state(state_path)
    if st.get("date") != today:
        st["date"], st["count"] = today, 0

    if not force:
        if now - st.get("last_sent", 0) < MIN_GAP_SEC:
            log("skip: 冷却中（距上次 %d 分钟）" % ((now - st["last_sent"]) / 60))
            return {"action": "skip", "reason": "cooldown", "sent": False}
        if st["count"] >= MAX_PER_DAY:
            log("skip: 今日已发 %d 条达上限" % st["count"])
            return {"action": "skip", "reason": "daily_cap", "sent": False}
        if (roll or random.random)() >= PROB:
            log("roll: 未触发")
            return {"action": "skip", "reason": "roll", "sent": False}

    msg = (llm_fn or llm_message)(build_context())
    if not msg or "[SKIP]" in msg:
        log("skip: 大肥鱼此刻没话说")
        return {"action": "skip", "reason": "empty", "sent": False}
    sender = send_private or (lambda text: botcommon.napcat_send_private(SISTER_QQ, text))
    sender(msg)
    st["last_sent"], st["count"] = now, st.get("count", 0) + 1
    save_state(st, state_path)
    log("SENT to 姐: " + msg.replace("\n", " ")[:100])
    return {"action": "sent", "reason": "sent", "sent": True}


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log("ERROR: %r" % e)
        raise SystemExit(1)
