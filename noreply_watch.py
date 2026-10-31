# -*- coding: utf-8 -*-
"""noreply_watch.py — 进消息但不回复（Planner 哑火）告警

扫 MaiBot jsonl 日志，检测「有入站、宽限期后仍无成功发送」或
「Planner 已结束本轮思考且邻近无工具=reply」；触发时私聊哥哥一次（幂等）。

调用：cron MaiBotNoreplyWatch 每 5 分钟。
用法：
    python3 noreply_watch.py              # 判定 + 必要时告警
    python3 noreply_watch.py --dry-run    # 只打印 JSON 判定，不发消息、不改锁态发送
    python3 noreply_watch.py --test       # 强制发一条测试告警（人工端到端，勿刷）
    python3 noreply_watch.py --window 15 --grace 4

网络边界：告警仅走 botcommon→NapCat 127.0.0.1:3002；读日志纯本地，无外网。
night_watch 约定绝不发 QQ——本脚本独立，勿把发信塞进 night_watch。
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "my-maibot" / "logs"
STATE_FILE = BASE / "my-maibot" / "data" / "noreply_watch_state.json"

OWNER_QQ = 2968874474  # 哥哥（与 token_guard 相同）

DEFAULT_WINDOW_MIN = 15
DEFAULT_GRACE_MIN = 4
DEFAULT_LOG_FILES = 8
# Planner 结束前/后多少秒内出现 工具=reply 视为同轮邻近（reply 通常在 end 之前）
PLANNER_REPLY_PROXIMITY_SEC = 120

RE_INBOUND = re.compile(r"检测到(?:群聊|私聊)消息，路由到 Maisaka")
RE_PLANNER_END = re.compile(r"Planner 已结束本轮思考")
RE_TOOL_REPLY = re.compile(r"工具=reply")
RE_SEND_OK = re.compile(r"\[SendService\] 成功发送消息")
RE_TS = re.compile(r"^(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$")
RE_LOG_DATE = re.compile(r"app_(\d{4})(\d{2})(\d{2})_")


def parse_ts(ts_str: str, year_hint: int) -> float | None:
    """把日志 timestamp（MM-DD HH:MM:SS）解析为 epoch；失败返回 None。"""
    m = RE_TS.match((ts_str or "").strip())
    if not m:
        return None
    month, day, hh, mm, ss = (int(x) for x in m.groups())
    try:
        return datetime(year_hint, month, day, hh, mm, ss).timestamp()
    except ValueError:
        return None


def year_from_log_name(path: Path) -> int:
    m = RE_LOG_DATE.search(path.name)
    if m:
        return int(m.group(1))
    return datetime.now().year


def classify_event(event: str) -> str | None:
    """返回 inbound / planner_end / tool_reply / send_ok 之一，否则 None。"""
    if not event:
        return None
    if RE_INBOUND.search(event):
        return "inbound"
    if RE_PLANNER_END.search(event):
        return "planner_end"
    if RE_TOOL_REPLY.search(event):
        return "tool_reply"
    if RE_SEND_OK.search(event):
        return "send_ok"
    return None


def load_events_from_logs(
    log_dir: Path,
    *,
    now: float,
    window_sec: float,
    max_files: int = DEFAULT_LOG_FILES,
) -> list[dict]:
    """读最近若干 app_*.log.jsonl，抽出窗口内相关事件。"""
    if not log_dir.is_dir():
        return []
    files = sorted(
        log_dir.glob("app_*.log.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_files]
    cutoff = now - window_sec - 3600  # 多读 1h 缓冲，判定时再裁窗
    out: list[dict] = []
    for path in files:
        year = year_from_log_name(path)
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = classify_event(str(obj.get("event") or ""))
                    if not kind:
                        continue
                    ts = parse_ts(str(obj.get("timestamp") or ""), year)
                    if ts is None or ts < cutoff:
                        continue
                    out.append(
                        {
                            "t": ts,
                            "kind": kind,
                            "event": str(obj.get("event") or "")[:240],
                            "module": obj.get("module"),
                            "log": path.name,
                        }
                    )
        except OSError as e:
            print(f"读日志失败 {path.name}: {e}")
    out.sort(key=lambda e: e["t"])
    return out


def planner_ends_without_reply(
    events: list[dict],
    *,
    proximity_sec: float = PLANNER_REPLY_PROXIMITY_SEC,
) -> list[dict]:
    """找出邻近窗口内没有 tool_reply 的 planner_end（同轮启发式）。"""
    replies = [e["t"] for e in events if e["kind"] == "tool_reply"]
    orphans = []
    for e in events:
        if e["kind"] != "planner_end":
            continue
        t = e["t"]
        # 健康路径：reply 在 end 之前几秒；允许少量滞后
        has = any((t - proximity_sec) <= rt <= (t + 30) for rt in replies)
        if not has:
            orphans.append(e)
    return orphans


def evaluate(
    events: list[dict],
    *,
    now: float,
    window_sec: float,
    grace_sec: float,
    proximity_sec: float = PLANNER_REPLY_PROXIMITY_SEC,
) -> dict:
    """纯函数：根据事件列表判定是否哑火。可测。

    哑火条件（满足其一即 dumb）：
    1) 窗口内有入站，距最近入站已超过宽限期，且窗口内无 SendService 成功；
    2) 窗口内有入站、无成功发送，且存在 Planner 结束且邻近无工具=reply。
    """
    win = [e for e in events if (now - window_sec) <= e["t"] <= now]
    inbounds = [e for e in win if e["kind"] == "inbound"]
    sends = [e for e in win if e["kind"] == "send_ok"]
    replies = [e for e in win if e["kind"] == "tool_reply"]
    planner_ends = [e for e in win if e["kind"] == "planner_end"]
    orphans = planner_ends_without_reply(win, proximity_sec=proximity_sec)

    last_inbound_t = max((e["t"] for e in inbounds), default=None)
    last_send_t = max((e["t"] for e in sends), default=None)
    inbound_age = (now - last_inbound_t) if last_inbound_t is not None else None

    silence_mute = (
        len(inbounds) > 0
        and inbound_age is not None
        and inbound_age >= grace_sec
        and len(sends) == 0
    )
    planner_mute = (
        len(inbounds) > 0
        and len(sends) == 0
        and len(orphans) > 0
        and inbound_age is not None
        and inbound_age >= grace_sec
    )
    # planner_mute 也要求宽限期，避免处理中误报；与 silence 重叠时仍标原因
    is_dumb = bool(silence_mute or planner_mute)

    # 幂等指纹：最近入站时间 + 窗口内无发送时的 orphan 数
    if last_inbound_t is not None:
        fingerprint = f"in:{int(last_inbound_t)}:sends:{len(sends)}:orph:{len(orphans)}"
    else:
        fingerprint = "healthy-no-inbound"

    return {
        "is_dumb": is_dumb,
        "silence_mute": silence_mute,
        "planner_mute": planner_mute,
        "window_sec": window_sec,
        "grace_sec": grace_sec,
        "inbound_count": len(inbounds),
        "send_ok_count": len(sends),
        "tool_reply_count": len(replies),
        "planner_end_count": len(planner_ends),
        "planner_end_no_reply_count": len(orphans),
        "last_inbound_age_sec": None if inbound_age is None else round(inbound_age, 1),
        "last_inbound_t": last_inbound_t,
        "last_send_t": last_send_t,
        "fingerprint": fingerprint,
        "sample_orphan_events": [o.get("event", "")[:120] for o in orphans[:3]],
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"state 损坏，重建: {e}")
    return {
        "alerting": False,
        "last_alert_ts": 0,
        "last_fingerprint": "",
        "last_verdict": {},
    }


def save_state(st: dict) -> None:
    botcommon.write_json_atomic(STATE_FILE, st, indent=2)


def format_alert(verdict: dict, *, now: float) -> str:
    win_m = int(verdict["window_sec"] // 60)
    age = verdict.get("last_inbound_age_sec")
    age_s = f"{age:.0f}s" if age is not None else "?"
    reasons = []
    if verdict.get("silence_mute"):
        reasons.append("静默(入站后无发送)")
    if verdict.get("planner_mute"):
        reasons.append("Planner结束无reply")
    reason = "+".join(reasons) or "哑火"
    ts = datetime.fromtimestamp(now).strftime("%H:%M:%S")
    return (
        f"哥！大肥鱼哑火警报（{ts}）[{reason}]："
        f"近{win_m}分钟入站{verdict['inbound_count']}、成功发送{verdict['send_ok_count']}、"
        f"Planner结束无reply {verdict['planner_end_no_reply_count']}；"
        f"距最近入站 {age_s}。快看 model/planner 池呜呜"
    )


def apply_alert_policy(
    st: dict,
    verdict: dict,
    *,
    dry_run: bool,
    force_test: bool,
    now: float,
) -> dict:
    """更新锁态并决定是否发送。返回动作摘要（纯逻辑，发送由调用方执行）。"""
    action = {
        "should_send": False,
        "clear_lock": False,
        "reason": "",
        "text": "",
    }
    if force_test:
        action["should_send"] = True
        action["reason"] = "test"
        action["text"] = (
            f"哥，noreply_watch --test 连通性检查（"
            f"{datetime.fromtimestamp(now).strftime('%H:%M:%S')}），忽略即可~"
        )
        return action

    if not verdict["is_dumb"]:
        if st.get("alerting"):
            action["clear_lock"] = True
            action["reason"] = "recovered"
        else:
            action["reason"] = "healthy"
        return action

    fp = verdict["fingerprint"]
    if st.get("alerting") and st.get("last_fingerprint") == fp:
        action["reason"] = "idempotent-skip"
        return action
    if st.get("alerting") and st.get("last_fingerprint") != fp:
        # 同一轮告警期内指纹变了（仍哑火）——仍跳过，等恢复后再告
        action["reason"] = "idempotent-skip-still-alerting"
        return action

    action["should_send"] = True
    action["reason"] = "new-mute"
    action["text"] = format_alert(verdict, now=now)
    return action


def commit_state_after_action(st: dict, verdict: dict, action: dict, *, now: float, dry_run: bool) -> dict:
    """按动作增量改 state；dry_run 时不把 alerting 锁打真（避免 dry 污染），但仍写 last_verdict。"""
    st = dict(st)
    st["last_verdict"] = {
        k: verdict[k]
        for k in (
            "is_dumb",
            "silence_mute",
            "planner_mute",
            "inbound_count",
            "send_ok_count",
            "planner_end_no_reply_count",
            "last_inbound_age_sec",
            "fingerprint",
        )
        if k in verdict
    }
    st["last_check_ts"] = now
    if dry_run:
        st["last_dry_run"] = st["last_verdict"]
        return st
    if action["reason"] == "test":
        # 测试不改 alerting 锁
        st["last_test_ts"] = now
        return st
    if action["clear_lock"]:
        st["alerting"] = False
        st["last_fingerprint"] = ""
        st["recovered_ts"] = now
    elif action["should_send"] and action["reason"] == "new-mute":
        st["alerting"] = True
        st["last_alert_ts"] = now
        st["last_fingerprint"] = verdict["fingerprint"]
    return st


def parse_args(argv: list[str]) -> dict:
    window = DEFAULT_WINDOW_MIN
    grace = DEFAULT_GRACE_MIN
    dry_run = "--dry-run" in argv
    force_test = "--test" in argv
    if "--window" in argv:
        i = argv.index("--window")
        window = float(argv[i + 1])
    if "--grace" in argv:
        i = argv.index("--grace")
        grace = float(argv[i + 1])
    return {
        "window_min": window,
        "grace_min": grace,
        "dry_run": dry_run,
        "force_test": force_test,
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts = parse_args(argv)
    now = time.time()
    window_sec = opts["window_min"] * 60
    grace_sec = opts["grace_min"] * 60

    events = load_events_from_logs(LOG_DIR, now=now, window_sec=window_sec)
    verdict = evaluate(
        events, now=now, window_sec=window_sec, grace_sec=grace_sec
    )
    st = load_state()

    # dry-run 用独立锁态，避免污染正式 alerting；连续 dry-run 仍可幂等跳过
    if opts["dry_run"] and not opts["force_test"]:
        policy_st = {
            "alerting": bool(st.get("dry_alerting")),
            "last_fingerprint": st.get("dry_fingerprint") or "",
            "last_alert_ts": st.get("dry_last_alert_ts") or 0,
        }
    else:
        policy_st = st

    action = apply_alert_policy(
        policy_st,
        verdict,
        dry_run=opts["dry_run"],
        force_test=opts["force_test"],
        now=now,
    )

    sent = False
    send_error = None
    if action["should_send"] and not opts["dry_run"]:
        try:
            botcommon.napcat_send_private(OWNER_QQ, action["text"])
            sent = True
            print("已私聊哥哥:", action["text"][:80])
        except Exception as e:
            send_error = str(e)[:200]
            print("发送失败:", send_error)

    # 发送失败不打 alerting 锁，下轮可重试
    if send_error and action["reason"] == "new-mute":
        action = dict(action)
        action["should_send"] = False
        action["reason"] = "send-failed"

    st2 = commit_state_after_action(
        st, verdict, action, now=now, dry_run=False
    )
    # 上面统一走正式 commit 字段；dry-run 再写 dry_* 并恢复正式 alerting
    if opts["dry_run"] and not opts["force_test"]:
        st2["alerting"] = st.get("alerting", False)
        st2["last_fingerprint"] = st.get("last_fingerprint", "")
        st2["last_alert_ts"] = st.get("last_alert_ts", 0)
        if action["clear_lock"]:
            st2["dry_alerting"] = False
            st2["dry_fingerprint"] = ""
        elif action["should_send"] and action["reason"] == "new-mute":
            st2["dry_alerting"] = True
            st2["dry_fingerprint"] = verdict["fingerprint"]
            st2["dry_last_alert_ts"] = now
        elif action["reason"].startswith("idempotent"):
            st2["dry_alerting"] = bool(st.get("dry_alerting"))
            st2["dry_fingerprint"] = st.get("dry_fingerprint") or ""
        st2["last_dry_run"] = st2.get("last_verdict")
    elif not opts["dry_run"] and action.get("clear_lock"):
        st2["dry_alerting"] = False
        st2["dry_fingerprint"] = ""

    try:
        save_state(st2)
    except Exception as e:
        print(f"state 落盘失败: {e}")

    out = {
        "ts": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": opts["dry_run"],
        "verdict": {
            "is_dumb": verdict["is_dumb"],
            "silence_mute": verdict["silence_mute"],
            "planner_mute": verdict["planner_mute"],
            "inbound_count": verdict["inbound_count"],
            "send_ok_count": verdict["send_ok_count"],
            "tool_reply_count": verdict["tool_reply_count"],
            "planner_end_count": verdict["planner_end_count"],
            "planner_end_no_reply_count": verdict["planner_end_no_reply_count"],
            "last_inbound_age_sec": verdict["last_inbound_age_sec"],
            "fingerprint": verdict["fingerprint"],
        },
        "action": {
            "reason": action["reason"],
            "would_send": bool(action["should_send"]),
            "sent": sent,
            "clear_lock": action["clear_lock"],
            "text_preview": (action["text"] or "")[:120],
            "send_error": send_error,
        },
        "alerting": st2.get("alerting"),
        "dry_alerting": st2.get("dry_alerting"),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
