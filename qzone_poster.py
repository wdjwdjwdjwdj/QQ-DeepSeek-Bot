# -*- coding: utf-8 -*-
"""qzone_poster.py — 大肥鱼朋友圈发送器（只发文案，不发图）

计划任务 MaiBotQzonePoster 每日 19:40 调用（py qzone_poster.py）：
读 daily_life.py 产出的 life_state.json 里今日 post_draft（朋友圈草稿），
经本机 onebot-qzone 桥接（OneBot v11 HTTP :5700）发 QQ 空间说说。
拟人节律：85% 概率发（留 15% "今天没发朋友圈"）；当天已发幂等跳过。
用法：py qzone_poster.py          （定时任务入口）
      py qzone_poster.py --test   （发一条"仅自己可见"的测试说说，人工验证用）
网络边界（见 botcommon.py）：仅本机 onebot-qzone 桥接 127.0.0.1 http。
"""
import json
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
LIFE_STATE = BASE / "life_state.json"
POST_STATE = BASE / "qzone_poster_state.json"

ALLOWED = {"127.0.0.1": {"http"}}
QZONE_API = "http://127.0.0.1:5700/send_private_msg"
POST_PROBABILITY = 15  # 保留 15% 概率"今天不发"，更像真人


def qzone_send(text, private=False):
    body = json.dumps({
        "message": [{"type": "text", "data": {"text": text}}],
        **({"who_can_see": 2} if private else {}),
    }).encode("utf-8")
    res = botcommon.http_json(QZONE_API, ALLOWED, data=body,
                              headers={"Content-Type": "application/json"}, timeout=60)
    if res.get("status") != "ok" or (res.get("retcode") not in (0, None) and res.get("data") is None):
        raise RuntimeError("qzone 桥接返回异常: " + json.dumps(res, ensure_ascii=False)[:200])
    return res


def load_post_state():
    if POST_STATE.exists():
        try:
            return json.loads(POST_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"date": "", "posted": False}


def main():
    test = "--test" in sys.argv
    today = datetime.now().strftime("%Y-%m-%d")
    st = load_post_state()
    if not test and st.get("date") == today and st.get("posted"):
        print("今天已发过朋友圈，跳过")
        return

    life = json.loads(LIFE_STATE.read_text(encoding="utf-8")) if LIFE_STATE.exists() else {}
    draft = (life.get("post_draft") or "").strip()
    if life.get("date") != today or not draft:
        print("今日无朋友圈草稿（时间线未生成？），跳过")
        return

    if test:
        text = f"[测试] {draft}"
        res = qzone_send(text, private=True)
        print("测试说说已发（仅自己可见）:", json.dumps(res.get("data", {}), ensure_ascii=False)[:120])
        return

    if secrets.randbelow(100) < POST_PROBABILITY:
        botcommon.write_json_atomic(POST_STATE, {"date": today, "posted": False, "skip": "概率跳过"})
        print("今天决定不发朋友圈（拟人节律）")
        return

    try:
        res = qzone_send(draft)
        botcommon.write_json_atomic(POST_STATE, {"date": today, "posted": True, "t": time.time()})
        print("朋友圈已发:", draft)
    except Exception as e:
        # 失败不标记 posted，下轮计划任务窗口内不会重试（每日一次），明日再战
        print("发送失败(跳过):", str(e)[:120])
        botcommon.write_json_atomic(POST_STATE, {"date": today, "posted": False, "error": str(e)[:120]})


if __name__ == "__main__":
    main()
