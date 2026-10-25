# -*- coding: utf-8 -*-
"""watchdog.py — 全链路守护（Linux 服务器版，每 2 分钟由 crontab 调用一次）

与 Windows 版的状态机语义一致，动作改为 systemctl：
1. NapCat 会话在线？（:3002 get_login_info）
   - 离线且单元 inactive → systemctl start napcat（带 150s 冷却，15 分钟强制重拉）
   - 离线但单元 active → 进程活着未登录（大概率等扫码）→ 触碰 WAITING_QR_SCAN.flag
     暂停后续拉起（防 systemd+watchdog 循环重启杀掉用户正要扫的码，07:05 事故教训）
2. 已登录但 :3001 未监听 → systemctl restart napcat（带冷却）
3. MaiBot 在？（:8001）→ 不在则 systemctl restart maibot（同构冷却/强制重拉）
事件记录：watchdog.log（含离线时间线，恢复后可见掉了多久）
"""
import json
import subprocess
import time
import urllib.error
from datetime import datetime
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
LOG = BASE / "watchdog.log"
STATE = BASE / "watchdog_state.json"
QR_WAIT_FLAG = BASE / "WAITING_QR_SCAN.flag"  # 存在=autoLogin凭据过期等人工扫码，拉起动作全部暂停

# 允许的固定本机目标（安全边界）
ALLOWED = {"127.0.0.1": {"http"}}

# NapCat 进程活着但未登录持续多久后视为"等扫码"（autoLogin 正常 30s 内完成，留足余量）
QR_WAIT_AFTER = 180


def http_get(url, timeout=6):
    return botcommon.http_json(url, ALLOWED, timeout=timeout)


def http_alive(url, timeout=6):
    """有 HTTP 响应就算活着（任何状态码都算，只有连接层失败才算挂）。"""
    try:
        http_get(url, timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except urllib.error.URLError:
        return False
    except Exception:
        return True


def port_open(port):
    import socket
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"napcat_down_since": 0, "maibot_down_since": 0, "napcat_last_start": 0, "maibot_last_start": 0}


RESTART_COOLDOWN = 150  # 拉起后 150 秒内不再重复拉
DOWN_RETRY_AFTER = 900  # 离线持续超过 15 分钟仍未恢复，强制再拉一次


def save_state(st):
    botcommon.write_json_atomic(STATE, st)


def qr_wait_active():
    """等人工扫码期间重启无法恢复，暂停一切拉起（防杀掉用户正要扫的码/实例堆积）。"""
    try:
        return time.time() - QR_WAIT_FLAG.stat().st_mtime < 86400
    except OSError:
        return False


def unit_active(name):
    r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
    return r.stdout.strip() == "active"


def systemctl(action, name):
    subprocess.run(["systemctl", action, name], capture_output=True)


def now():
    return time.time()


def main():
    st = load_state()
    actions = []

    # 1. NapCat 会话在线检查（能查到登录信息=QQ会话活着）
    napcat_ok = False
    try:
        r = http_get("http://127.0.0.1:3002/get_login_info")
        napcat_ok = r.get("status") == "ok"
    except Exception:
        napcat_ok = False

    if not napcat_ok:
        if qr_wait_active():
            log("等待人工扫码，跳过 NapCat 自愈（重启无法恢复，避免干扰扫码）")
        elif unit_active("napcat"):
            # 进程活着但未登录：systemd 已保证进程在，重启无济于事（要人工扫码）
            if st["napcat_down_since"] == 0:
                st["napcat_down_since"] = now()
                log("ALERT: NapCat 进程 active 但未登录，观察中")
            elif now() - st["napcat_down_since"] > QR_WAIT_AFTER and not QR_WAIT_FLAG.exists():
                QR_WAIT_FLAG.touch()
                log("ALERT: NapCat 未登录超过 %d 秒，判定为等待扫码，触碰 %s 暂停拉起（扫码成功后自动清除）" % (QR_WAIT_AFTER, QR_WAIT_FLAG.name))
        else:
            if st["napcat_down_since"] == 0:
                st["napcat_down_since"] = now()
                log("ALERT: NapCat 会话离线且单元未运行，开始拉起流程")
            if now() - st["napcat_down_since"] > DOWN_RETRY_AFTER and now() - st.get("napcat_last_start", 0) > RESTART_COOLDOWN:
                log("WARN: NapCat 离线超过15分钟未自愈，强制重启单元")
                systemctl("restart", "napcat")
                st["napcat_last_start"] = now()
                st["napcat_down_since"] = now()
                actions.append("napcat-restart-again")
            elif now() - st.get("napcat_last_start", 0) > RESTART_COOLDOWN:
                systemctl("start", "napcat")
                st["napcat_last_start"] = now()
                actions.append("napcat-start")
    else:
        if st["napcat_down_since"]:
            down_min = int((now() - st["napcat_down_since"]) / 60)
            log(f"RECOVERED: NapCat 会话恢复（掉线约{down_min}分钟）")
            st["napcat_down_since"] = 0
        if QR_WAIT_FLAG.exists():
            QR_WAIT_FLAG.unlink(missing_ok=True)
            log("扫码恢复，标记已清，watchdog 回归正常巡逻")

    # 2. 已登录但 :3001 未监听（WS 服务端故障，重启可修复的真实故障）
    if napcat_ok and not port_open(3001) and not qr_wait_active():
        if now() - st.get("napcat_last_start", 0) > RESTART_COOLDOWN:
            log("ALERT: 已登录但 :3001 未监听，重启 napcat 单元")
            systemctl("restart", "napcat")
            st["napcat_last_start"] = now()
            actions.append("napcat-port-fix")

    # 3. MaiBot（与 NapCat 分支同构：首次记录+拉起，持续离线超时后强制重拉）
    maibot_ok = http_alive("http://127.0.0.1:8001")

    if not maibot_ok:
        if st["maibot_down_since"] == 0:
            st["maibot_down_since"] = now()
            if now() - st.get("maibot_last_start", 0) > RESTART_COOLDOWN:
                log("ALERT: MaiBot 无响应，重启 maibot 单元")
                systemctl("restart", "maibot")
                st["maibot_last_start"] = now()
                actions.append("maibot-restart")
            else:
                log("ALERT: MaiBot 无响应（冷却期内，等待下次检查）")
        elif now() - st["maibot_down_since"] > DOWN_RETRY_AFTER and now() - st.get("maibot_last_start", 0) > RESTART_COOLDOWN:
            log("WARN: MaiBot 离线超过15分钟未自愈，再次强制重启")
            systemctl("restart", "maibot")
            st["maibot_last_start"] = now()
            st["maibot_down_since"] = now()
            actions.append("maibot-restart-again")
    else:
        if st["maibot_down_since"]:
            down_min = int((now() - st["maibot_down_since"]) / 60)
            log(f"RECOVERED: MaiBot 恢复（掉线约{down_min}分钟）")
            st["maibot_down_since"] = 0

    save_state(st)
    print("check done; actions:", actions or "none")


if __name__ == "__main__":
    main()
