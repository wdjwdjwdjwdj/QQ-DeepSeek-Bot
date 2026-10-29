# -*- coding: utf-8 -*-
"""token_guard.py — 大肥鱼余额守护 + 付费日预算门控

定时查 DeepSeek 余额，低于阈值时以大肥鱼口吻私聊哥哥求救；
并按 budget_config.json 的日预算硬顶，超预算后把主聊/工具池里的
DeepSeek 踢掉（planner 保留，保证 tool_call），只走免费池。

用法（cron MaiBotTokenGuard 每 10 分钟、MaiBotDailyReport 每天 21 点）：
    python3 token_guard.py                 # 查余额 + 求救 + 日预算 enforce
    python3 token_guard.py --test          # 强制触发求救（端到端测试）
    python3 token_guard.py --threshold 5
    python3 token_guard.py --report        # 当日账单日报（含预算行）
    python3 token_guard.py --budget-status # 只打印日耗/预算/锁态，不改配置
    python3 token_guard.py --budget-only   # 只 enforce 日预算（用已有轨迹+现查余额）
    python3 token_guard.py --dry-run       # enforce 只打印将改什么，不写盘不重启

凭据说明：API Key 从 MaiBot 的 model_config.toml 读取，不在本文件硬编码。
网络边界：仅允许两个固定目标（DeepSeek 官方 API、本机 NapCat 127.0.0.1:3002），
白名单外一律拒绝，且禁止跟随重定向（见 botcommon.py）。
"""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "my-maibot" / "data" / "token_guard_state.json"
MODEL_CFG = BASE / "my-maibot" / "config" / "model_config.toml"

OWNER_QQ = 2968874474          # 哥哥（收求救/预算消息）
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
BUDGET_LOCK_MSGS = [
    "哥，今日 DeepSeek 预算 ¥{budget:.2f} 已用完（花了 ¥{spend:.2f}）。主聊改纯免费，Planner 仍留付费兜底防哑火。",
    "鱼粮日预算到顶啦（¥{spend:.2f}/¥{budget:.2f}）~ 回复走免费池，规划还能用 DeepSeek，明天再开闸。",
]
BUDGET_UNLOCK_MSGS = [
    "哥，日预算闸门打开了（今日 DeepSeek ¥{spend:.2f}/¥{budget:.2f}），主聊付费兜底已恢复~",
    "预算还够（¥{spend:.2f}/¥{budget:.2f}），付费回复兜底归队，咕噜~",
]


def http_request(url, data=None, headers=None):
    return botcommon.http_json(url, ALLOWED_TARGETS, data=data, headers=headers, timeout=30)


def probe_free_pools():
    """只探测免费池是否可用（各最多 1 次、max_tokens=8），不调用 DeepSeek chat。"""
    status = {}
    try:
        text, provider, model = botcommon.llm_free_chat(
            "说一个字：好", temperature=0, timeout=30, allow_paid=False, max_tokens=8
        )
        status["ok"] = True
        status["provider"] = provider
        status["model"] = model
        status["sample"] = (text or "")[:20]
    except Exception as e:
        status["ok"] = False
        status["error"] = str(e)[:160]
    print("免费池探测:", status)
    return status


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
    """常规轨迹记录：只保留当天、最多 96 个点。返回(更新后state, 今日首点余额)。"""
    today = time.strftime("%Y-%m-%d")
    log = [e for e in st.get("daily_log", []) if e["day"] == today]
    st["daily_log"] = (log[-96:] if log else []) + [{"day": today, "t": time.time(), "bal": balance}]
    st["daily_log"] = st["daily_log"][-96:]
    return st, (log[0]["bal"] if log else None)


def _fmt_list(lst):
    return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in lst) + "]"


def set_task_model_lists(toml_text: str, updates: dict[str, list]) -> str:
    """在 model_config.toml 文本里替换指定 task 的 model_list = [...]（保留段内其它行）。"""
    out = toml_text
    for task, new_list in updates.items():
        pat = re.compile(
            rf"(\[model_task_config\.{re.escape(task)}\][^\[]*?model_list\s*=\s*)\[[^\]]*\]",
            re.DOTALL,
        )
        m = pat.search(out)
        if not m:
            raise RuntimeError(f"找不到 model_task_config.{task} 的 model_list 锚点")
        out = pat.sub(m.group(1) + _fmt_list(new_list), out, count=1)
    return out


def read_task_lists(cfg: dict) -> dict[str, list]:
    mtc = cfg.get("model_task_config") or {}
    return {
        t: list((mtc.get(t) or {}).get("model_list") or [])
        for t in mtc
        if isinstance(mtc.get(t), dict) and "model_list" in mtc[t]
    }


def compute_gated_lists(current: dict[str, list], cfg: dict, locked: bool) -> dict[str, list]:
    """按锁态计算 gated_tasks 的目标列表；未门控任务不返回。"""
    paid = list(cfg["paid_models"])
    out = {}
    for task in cfg["gated_tasks"]:
        cur = list(current.get(task) or [])
        if locked:
            out[task] = botcommon.strip_paid_from_list(cur, paid)
        else:
            # 恢复：若当前已无付费，补回 deepseek-flash-reply（默认）；保留已有付费顺序
            have_paid = [m for m in cur if m in set(paid)]
            to_add = have_paid or [m for m in ("deepseek-flash-reply", "deepseek-flash") if m in set(paid)][:1]
            out[task] = botcommon.restore_paid_into_list(cur, to_add, paid)
    return out


def apply_model_lists(updates: dict[str, list], dry_run: bool = False) -> bool:
    """写回 model_config；有实际变化返回 True。dry_run 不写盘。"""
    if not MODEL_CFG.is_file():
        raise FileNotFoundError(str(MODEL_CFG))
    raw = MODEL_CFG.read_text(encoding="utf-8")
    cfg = tomllib.loads(raw)
    cur = read_task_lists(cfg)
    changed = {}
    for task, new_list in updates.items():
        if cur.get(task) != new_list:
            changed[task] = new_list
            print(f"  {task}: {cur.get(task)} -> {new_list}")
    if not changed:
        print("model_list 无变化")
        return False
    if dry_run:
        print("dry-run: 不写盘")
        return True
    new_text = set_task_model_lists(raw, changed)
    # 校验
    parsed = tomllib.loads(new_text)
    for task, new_list in changed.items():
        got = parsed["model_task_config"][task]["model_list"]
        if got != new_list:
            raise RuntimeError(f"写回校验失败 {task}: {got} != {new_list}")
    # planner 保护：不得误改
    if cfg["model_task_config"]["planner"]["model_list"] != parsed["model_task_config"]["planner"]["model_list"]:
        raise RuntimeError("拒绝：planner.model_list 被意外改动")
    tmp = MODEL_CFG.with_suffix(".toml.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(MODEL_CFG)
    print("已原子写回", MODEL_CFG)
    return True


def restart_maibot():
    r = subprocess.run(["systemctl", "restart", "maibot"], capture_output=True, text=True)
    print("systemctl restart maibot:", "ok" if r.returncode == 0 else f"rc={r.returncode} {r.stderr[:120]}")
    return r.returncode == 0


def enforce_budget_gate(st, balance, *, dry_run=False, notify=True):
    """根据当日消耗硬顶，踢/恢复主聊池付费模型。返回 (locked, changed, info)。"""
    bcfg = botcommon.load_budget_config()
    spend, first = botcommon.today_deepseek_spend(st.get("daily_log", []), balance)
    budget = float(bcfg["daily_cny"])
    enabled = bool(bcfg.get("enabled", True))
    want_lock = enabled and spend >= budget
    prev = st.get("budget_lock") or {}
    was_locked = bool(prev.get("active"))

    info = {
        "enabled": enabled,
        "spend": spend,
        "budget": budget,
        "first_bal": first,
        "balance": balance,
        "want_lock": want_lock,
        "was_locked": was_locked,
    }
    print(
        f"日预算: spend=¥{spend:.2f} / budget=¥{budget:.2f} "
        f"enabled={enabled} want_lock={want_lock} was_locked={was_locked}"
    )

    if not enabled:
        # 若曾锁过且配置关掉，尝试恢复
        want_lock = False

    raw = MODEL_CFG.read_text(encoding="utf-8")
    cfg = tomllib.loads(raw)
    current = read_task_lists(cfg)
    target = compute_gated_lists(current, bcfg, locked=want_lock)

    # keep_planner_paid：显式断言不碰 planner
    if bcfg.get("keep_planner_paid", True):
        planner = current.get("planner") or []
        if "deepseek-flash-planner" not in planner and not any("deepseek" in m for m in planner):
            print("WARN: planner 池已无 DeepSeek，方向1白名单可能被破坏")

    changed = apply_model_lists(target, dry_run=dry_run)
    if changed and not dry_run and bcfg.get("restart_on_change", True):
        restart_maibot()

    # 状态机 + 通知（仅锁态翻转时）
    today = time.strftime("%Y-%m-%d")
    st["budget_lock"] = {
        "active": want_lock,
        "day": today,
        "spend": spend,
        "budget": budget,
        "updated_at": time.time(),
    }
    if notify and bcfg.get("notify", True) and not dry_run and want_lock != was_locked:
        if want_lock:
            msg = secrets.choice(BUDGET_LOCK_MSGS).format(budget=budget, spend=spend)
        else:
            msg = secrets.choice(BUDGET_UNLOCK_MSGS).format(budget=budget, spend=spend)
        try:
            send_qq(msg)
        except Exception as e:
            print("预算通知发送失败:", e)

    info["changed"] = changed
    info["locked"] = want_lock
    return want_lock, changed, info


def budget_status_line(st, balance):
    bcfg = botcommon.load_budget_config()
    spend, first = botcommon.today_deepseek_spend(st.get("daily_log", []), balance)
    locked = botcommon.is_paid_budget_locked(st.get("daily_log", []), balance, bcfg)
    active = bool((st.get("budget_lock") or {}).get("active"))
    return (
        f"预算¥{bcfg['daily_cny']:.2f} 今日花¥{spend:.2f} "
        f"锁态={'ON' if locked else 'OFF'}(state={'ON' if active else 'OFF'}) "
        f"enabled={bcfg.get('enabled', True)}"
    )


def daily_report(balance, key):
    """记录余额轨迹；计算当日消耗并发账单。"""
    st = load_state()
    st, first_bal = record_balance(st, balance)
    # 先 enforce 再读锁态（报告日也要硬顶）
    try:
        enforce_budget_gate(st, balance, dry_run=False, notify=True)
    except Exception as e:
        print("日报中预算门控失败:", e)
    msg = f"哥~今日账单来啦：当前 token 余额 {balance:.2f} 元"
    if first_bal is not None:
        msg += f"，今天花了 {first_bal - balance:.2f} 元"
    bcfg = botcommon.load_budget_config()
    spend, _ = botcommon.today_deepseek_spend(st.get("daily_log", []), balance)
    msg += f"。日预算 ¥{bcfg['daily_cny']:.2f}，已用 ¥{spend:.2f}"
    if (st.get("budget_lock") or {}).get("active"):
        msg += "（已锁付费主聊）"
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
    budget_only = False
    budget_status = False
    dry_run = False
    args = sys.argv[1:]
    if "--test" in args:
        test = True
    if "--report" in args:
        report = True
    if "--budget-only" in args:
        budget_only = True
    if "--budget-status" in args:
        budget_status = True
    if "--dry-run" in args:
        dry_run = True
    if "--threshold" in args:
        i = args.index("--threshold")
        if i + 1 >= len(args):
            raise SystemExit("用法: python3 token_guard.py --threshold 数字")
        threshold = float(args[i + 1])

    key = botcommon.load_provider_key("DeepSeek")
    balance = get_balance(key)
    print(f"当前 DeepSeek 余额: {balance:.2f} 元（阈值 {threshold}）")

    if report:
        daily_report(balance, key)
        return

    st = load_state()
    st, _ = record_balance(st, balance)

    if budget_status:
        print(budget_status_line(st, balance))
        save_state(st)
        return

    # 日预算硬顶（主路径与 --budget-only）
    try:
        enforce_budget_gate(st, balance, dry_run=dry_run, notify=not dry_run)
    except Exception as e:
        print("预算门控失败（余额轨迹仍保存）:", e)
    save_state(st)

    if budget_only:
        print(budget_status_line(st, balance))
        return

    now = time.time()

    if test or balance < threshold:
        if not test and st.get("alerting") and now - st.get("last_remind", 0) < REMIND_COOLDOWN:
            print("警报期内已提醒过，冷却中，跳过")
            return
        msg = secrets.choice(HELP_MSGS).format(b=f"{balance:.2f}")
        send_qq(msg)
        st["alerting"] = True
        st["last_remind"] = now
        save_state(st)
    elif st.get("alerting"):
        send_qq(secrets.choice(RECOVER_MSGS))
        st["alerting"] = False
        st["last_remind"] = 0
        save_state(st)
    else:
        print("余额充足，无事发生")


if __name__ == "__main__":
    main()
