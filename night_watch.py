# -*- coding: utf-8 -*-
"""night_watch.py — 服务器迁移前夜兜底监控（ZCode 定时自动化调用，未注册 Windows 计划任务）

功能：
1. 只读体检：NapCat 会话(:3002 get_login_info)、端口 3001/8001/5700、
   watchdog_state.json 新鲜度、bot_config.toml 可解析、scheduled_sender_state.json 完整；
2. 兜底自愈：仅当 watchdog 自身失效（state 超 15 分钟未更新）时，重新启用
   MaiBotWatchdog 计划任务并立即代跑一次 watchdog.py（进程级自愈由此覆盖）；
   其余情况一律不动手，交给 watchdog 每 2 分钟的常规自愈，避免双重守护打架；
3. --preflight 模式：追加定时消息就绪清单（enabled 任务的 once 发送状态、
   机器人在目标群的群内身份）。

用法：
    py night_watch.py              # 体检 + 兜底；stdout 一行 JSON；退出码 0=健康 1=异常
    py night_watch.py --preflight  # 追加发送就绪检查

网络边界：仅 127.0.0.1 只读 API（get_login_info / get_group_member_info），
绝不发送任何 QQ 消息（测试零打扰群聊是用户红线）；无外网请求。
"""
import json
import socket
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
WATCHDOG_STATE = BASE / "watchdog_state.json"
WATCHDOG_STALE_SEC = 900  # watchdog 每 2 分钟落盘一次，state 超 15 分钟视为守护失效
SENDER_STATE = BASE / "scheduled_sender_state.json"
BOT_CONFIG = BASE / "my-maibot" / "config" / "bot_config.toml"
SCHEDULE_CONFIG = BASE / "schedule_config.json"
ALLOWED = {"127.0.0.1": {"http"}}

PORTS = {"napcat_ws": 3001, "maibot_webui": 8001, "qzone_bridge": 5700}


def port_open(port):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def napcat_uid():
    """NapCat 会话在线时返回机器人 QQ 号，离线返回 0。"""
    try:
        r = botcommon.http_json("http://127.0.0.1:3002/get_login_info", ALLOWED, timeout=6)
        if r.get("status") == "ok":
            return int(r["data"]["user_id"])
    except Exception:
        pass
    return 0


def group_role(uid, group_id):
    try:
        url = f"http://127.0.0.1:3002/get_group_member_info?group_id={group_id}&user_id={uid}"
        return botcommon.http_json(url, ALLOWED, timeout=6).get("data", {}).get("role", "?")
    except Exception:
        return "?"


def revive_watchdog(actions):
    """watchdog 失效兜底：重新启用计划任务 + 立即代跑一次（进程自愈随之覆盖）。"""
    r = subprocess.run(["schtasks", "/change", "/tn", "MaiBotWatchdog", "/enable"],
                       capture_output=True, text=True)
    actions.append("schtasks-enable-watchdog:" + ("ok" if r.returncode == 0 else "fail"))
    try:
        r2 = subprocess.run([sys.executable, str(BASE / "watchdog.py")],
                            cwd=str(BASE), capture_output=True, text=True, timeout=180)
        actions.append("watchdog-once:" + ("ok" if r2.returncode == 0 else f"rc={r2.returncode}"))
    except Exception as e:
        actions.append(f"watchdog-once:error:{e}")


def preflight(uid):
    """enabled 的定时任务就绪清单：once 任务报已发/未发，群任务报机器人身份。"""
    cfg = json.loads(SCHEDULE_CONFIG.read_text(encoding="utf-8"))
    try:
        sent_keys = json.loads(SENDER_STATE.read_text(encoding="utf-8")).get("sent", [])
    except Exception:
        sent_keys = []
    out = []
    for t in cfg.get("tasks", []):
        if not t.get("enabled"):
            continue
        once = str(t.get("once") or "").strip()
        info = {"name": t.get("name"), "type": t["target"]["type"],
                "id": t["target"]["id"], "once": once or None}
        if once:
            info["already_sent"] = f"once {once} {t.get('name')}" in sent_keys
        if t["target"]["type"] == "group":
            info["bot_role"] = group_role(uid, t["target"]["id"])
        out.append(info)
    return out


def main():
    preflight_mode = "--preflight" in sys.argv[1:]
    checks, actions, problems = {}, [], []

    uid = napcat_uid()
    checks["napcat_session"] = bool(uid)
    for name, port in PORTS.items():
        checks[name] = port_open(port)

    # watchdog 新鲜度是兜底自愈的唯一触发点
    fresh = WATCHDOG_STATE.exists() and (time.time() - WATCHDOG_STATE.stat().st_mtime) < WATCHDOG_STALE_SEC
    checks["watchdog_fresh"] = fresh
    if not fresh:
        revive_watchdog(actions)
        uid = napcat_uid()
        checks["napcat_session"] = bool(uid)
        checks["napcat_ws"] = port_open(PORTS["napcat_ws"])
        checks["maibot_webui"] = port_open(PORTS["maibot_webui"])

    # cognition 每 10 分钟改写 bot_config.toml，这里防注入写坏导致下次重启炸
    try:
        with open(BOT_CONFIG, "rb") as f:
            tomllib.load(f)
        checks["bot_config_valid"] = True
    except Exception as e:
        checks["bot_config_valid"] = False
        problems.append(f"bot_config.toml 解析失败: {e}")

    # planner 首位必须会 tool call；降本误配会导致「进消息但不回复」
    try:
        import verify_planner_pool as vpp
        data = vpp._load()
        pp_problems = vpp.check_static(data)
        checks["planner_pool_ok"] = not pp_problems
        for p in pp_problems:
            problems.append(f"planner_pool: {p}")
    except Exception as e:
        checks["planner_pool_ok"] = False
        problems.append(f"planner_pool check error: {e}")

    try:
        json.loads(SENDER_STATE.read_text(encoding="utf-8"))
        checks["sender_state_valid"] = True
    except Exception as e:
        checks["sender_state_valid"] = False
        problems.append(f"scheduled_sender_state.json 损坏: {e}")


    # 只读：最近一次 noreply_watch 判定（本脚本绝不发 QQ）
    noreply_state = BASE / "my-maibot" / "data" / "noreply_watch_state.json"
    try:
        if noreply_state.is_file():
            ns = json.loads(noreply_state.read_text(encoding="utf-8"))
            checks["noreply_alerting"] = bool(ns.get("alerting"))
            lv = ns.get("last_verdict") or {}
            checks["noreply_last_dumb"] = bool(lv.get("is_dumb"))
            if ns.get("alerting"):
                problems.append(
                    f"noreply_watch alerting fingerprint={ns.get('last_fingerprint', '')}"
                )
        else:
            checks["noreply_alerting"] = False
            checks["noreply_last_dumb"] = False
    except Exception as e:
        checks["noreply_state_ok"] = False
        problems.append(f"noreply_watch_state read error: {e}")

    if preflight_mode:
        checks["preflight"] = preflight(uid) if uid else "napcat_offline"

    critical = (not checks["napcat_session"] or not checks["maibot_webui"]
                or not checks["bot_config_valid"] or not checks["sender_state_valid"]
                or not checks.get("planner_pool_ok", True))
    verdict = "CRITICAL" if critical else ("WARN" if actions or problems or not checks["qzone_bridge"] else "OK")

    print(json.dumps({
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "verdict": verdict, "checks": checks, "actions": actions, "problems": problems,
    }, ensure_ascii=False))
    raise SystemExit(1 if critical else 0)


if __name__ == "__main__":
    main()
