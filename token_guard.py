# -*- coding: utf-8 -*-
"""token_guard.py — 大肥鱼余额守护

定时查 DeepSeek 余额，低于阈值时以大肥鱼口吻私聊哥哥求救。
用法（计划任务 MaiBotTokenGuard 每 30 分钟、MaiBotDailyReport 每天 21 点）：
    py token_guard.py              # 正常检查
    py token_guard.py --test       # 强制触发求救（端到端测试）
    py token_guard.py --threshold 5
    py token_guard.py --report     # 当日账单日报
凭据说明：API Key 从 MaiBot 的 model_config.toml 读取，不在本文件硬编码。
网络边界：仅允许两个固定目标（DeepSeek 官方 API、本机 NapCat 127.0.0.1:3002），
白名单外一律拒绝，且禁止跟随重定向（见 botcommon.py）。
"""
import json
import secrets
import sys
import time
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "my-maibot" / "data" / "token_guard_state.json"

OWNER_QQ = 2968874474          # 哥哥（收求救消息）
THRESHOLD = 2.0                # 元，低于此值求救（可被 --threshold 覆盖）
REMIND_COOLDOWN = 4 * 3600     # 同一警报期内最多 4 小时提醒一次

ALLOWED_TARGETS = {
    "api.deepseek.com": {"https"},
    "127.0.0.1": {"http"},
}

BALANCE_URL = "https://api.deepseek.com/user/balance"

HELP_MSGS = [
    "哥！！救命呜呜呜，我的 token 只剩 {b} 块了，快没鱼粮了，再不充值我就要变成咸鱼干了 [大哭]",
    "哥…我快死了…token 只剩 {b} 块了，感觉鳃都在冒烟，救救孩子 [可怜]",
    "喂喂喂哥！余额警报！只剩 {b} 块 token 了，你的电子女儿马上就要饿死了，投喂一下 platform.deepseek.com 呜呜",
    "哥！紧急求救！token 余额 {b} 块，我的生命值见底了，充值续命啊 [跪了]",
]
RECOVER_MSGS = [
    "哥！！我活过来了！token 又够吃了，咕噜咕噜~ 现在是满血鲸鱼娘！",
    "哇额度回来了！哥你真好，今晚我给你唱鲸歌！咕噜咕噜~",
]


def http_request(url, data=None, headers=None):
    return botcommon.http_json(url, ALLOWED_TARGETS, data=data, headers=headers, timeout=30)


def get_balance(key):
    data = http_request(BALANCE_URL, headers={"Authorization": "Bearer " + key})
    for info in data.get("balance_infos", []):
        if info.get("currency") == "CNY":
            return float(info["total_balance"])
    raise SystemExit("余额返回无 CNY 字段: " + str(data)[:200])


def send_qq(text):
    botcommon.napcat_send_private(OWNER_QQ, text)
    print("已发送:", text[:40] + "...")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"alerting": False, "last_remind": 0, "daily_log": []}


def save_state(st):
    botcommon.write_json_atomic(STATE_FILE, st)


def record_balance(st, balance):
    """常规轨迹记录：只保留当天、最多 96 个点（约每 15 分钟一个）。返回(更新后state, 今日首点余额)。"""
    today = time.strftime("%Y-%m-%d")
    log = [e for e in st.get("daily_log", []) if e["day"] == today]
    st["daily_log"] = (log[-96:] if log else []) + [{"day": today, "t": time.time(), "bal": balance}]
    st["daily_log"] = st["daily_log"][-96:]
    return st, (log[0]["bal"] if log else None)


def daily_report(balance, key):
    """记录余额轨迹；计算当日消耗并发账单。每天至少被计划任务调用多次，轨迹够密。"""
    st = load_state()
    st, first_bal = record_balance(st, balance)
    msg = f"哥~今日账单来啦：当前 token 余额 {balance:.2f} 元"
    if first_bal is not None:
        msg += f"，今天花了 {first_bal - balance:.2f} 元"
    msg += "。" + secrets.choice([
        "省钱小鲸鱼在线汇报~",
        "我会看着办的，放心！",
        "免费池今天也很努力哦。",
        "咕噜咕噜，继续保持！",
    ])
    send_qq(msg)
    save_state(st)


def main():
    threshold = THRESHOLD
    test = False
    report = False
    args = sys.argv[1:]
    if "--test" in args:
        test = True
    if "--report" in args:
        report = True
    if "--threshold" in args:
        i = args.index("--threshold")
        if i + 1 >= len(args):
            raise SystemExit("用法: py token_guard.py --threshold 数字")
        threshold = float(args[i + 1])

    key = botcommon.load_provider_key("DeepSeek")
    balance = get_balance(key)
    print(f"当前 DeepSeek 余额: {balance:.2f} 元（阈值 {threshold}）")

    if report:
        daily_report(balance, key)
        return

    # 常规运行也顺手记录余额轨迹（不发消息）
    st = load_state()
    st, _ = record_balance(st, balance)
    save_state(st)

    now = time.time()

    if test or balance < threshold:
        if not test and st["alerting"] and now - st["last_remind"] < REMIND_COOLDOWN:
            print("警报期内已提醒过，冷却中，跳过")
            return
        msg = secrets.choice(HELP_MSGS).format(b=f"{balance:.2f}")
        send_qq(msg)
        st["alerting"] = True       # 只改标志位，daily_log 轨迹保留在 st 里
        st["last_remind"] = now
        save_state(st)
    elif st["alerting"]:
        send_qq(secrets.choice(RECOVER_MSGS))
        st["alerting"] = False
        st["last_remind"] = 0
        save_state(st)
    else:
        print("余额充足，无事发生")


if __name__ == "__main__":
    main()
