# -*- coding: utf-8 -*-
"""校验 noreply_watch 哑火判定纯函数（不发 QQ、不读生产日志）。

用法：
  python3 verify_noreply_watch.py

网络边界：不出网、不发消息。
"""
from __future__ import annotations

import noreply_watch as nw


def _ev(t: float, kind: str, event: str = "") -> dict:
    defaults = {
        "inbound": "[群聊]检测到群聊消息，路由到 Maisaka",
        "planner_end": "[AI元创社] Planner 已结束本轮思考",
        "tool_reply": "[AI元创社] [正文工具调用] Planner 1/1: 工具=reply 调用ID=x",
        "send_ok": "[SendService] 成功发送消息到 abc",
    }
    return {"t": t, "kind": kind, "event": event or defaults[kind], "module": "t", "log": "t"}


def test_classify():
    assert nw.classify_event("[群聊]检测到群聊消息，路由到 Maisaka") == "inbound"
    assert nw.classify_event("[私聊]检测到私聊消息，路由到 Maisaka") == "inbound"
    assert nw.classify_event("[江湖骗子的私聊] Planner 已结束本轮思考") == "planner_end"
    assert nw.classify_event("[AI元创社] Planner 已结束本轮思考") == "planner_end"
    assert nw.classify_event("[AI元创社] [正文工具调用] Planner 1/1: 工具=reply 调用ID=1") == "tool_reply"
    assert nw.classify_event("[SendService] 成功发送消息到 xyz") == "send_ok"
    assert nw.classify_event("[SendService] 发送text消息到 xyz") is None
    print("OK classify")


def test_dumb_silence():
    now = 1_000_000.0
    # 入站在 5 分钟前，宽限 4 分钟，窗口内无发送 → 哑火
    events = [_ev(now - 300, "inbound")]
    v = nw.evaluate(events, now=now, window_sec=15 * 60, grace_sec=4 * 60)
    assert v["is_dumb"] is True, v
    assert v["silence_mute"] is True, v
    assert v["inbound_count"] == 1 and v["send_ok_count"] == 0
    print("OK dumb silence")


def test_healthy_inbound_reply_send():
    now = 1_000_000.0
    events = [
        _ev(now - 300, "inbound"),
        _ev(now - 295, "tool_reply"),
        _ev(now - 290, "send_ok"),
        _ev(now - 288, "planner_end"),
    ]
    v = nw.evaluate(events, now=now, window_sec=15 * 60, grace_sec=4 * 60)
    assert v["is_dumb"] is False, v
    assert v["silence_mute"] is False
    assert v["planner_mute"] is False
    assert v["planner_end_no_reply_count"] == 0
    print("OK healthy inbound+reply+send")


def test_dumb_planner_no_reply():
    now = 1_000_000.0
    # 私聊进 → Planner 结束无 reply → 无发送；已过宽限
    events = [
        _ev(now - 360, "inbound", "[私聊]检测到私聊消息，路由到 Maisaka"),
        _ev(now - 350, "planner_end", "[江湖骗子的私聊] Planner 已结束本轮思考"),
    ]
    v = nw.evaluate(events, now=now, window_sec=15 * 60, grace_sec=4 * 60)
    assert v["is_dumb"] is True, v
    assert v["planner_mute"] is True, v
    assert v["planner_end_no_reply_count"] == 1
    print("OK dumb planner no reply")


def test_within_grace_not_dumb():
    now = 1_000_000.0
    # 入站仅 60s 前，仍在处理中
    events = [
        _ev(now - 60, "inbound"),
        _ev(now - 50, "planner_end"),
    ]
    v = nw.evaluate(events, now=now, window_sec=15 * 60, grace_sec=4 * 60)
    assert v["is_dumb"] is False, v
    print("OK within grace not dumb")


def test_idempotent_policy():
    now = 1_000_000.0
    events = [_ev(now - 300, "inbound")]
    v = nw.evaluate(events, now=now, window_sec=15 * 60, grace_sec=4 * 60)
    assert v["is_dumb"]
    st = {"alerting": False, "last_fingerprint": "", "last_alert_ts": 0}
    a1 = nw.apply_alert_policy(st, v, dry_run=False, force_test=False, now=now)
    assert a1["should_send"] is True and a1["reason"] == "new-mute", a1
    st2 = nw.commit_state_after_action(st, v, a1, now=now, dry_run=False)
    assert st2["alerting"] is True
    a2 = nw.apply_alert_policy(st2, v, dry_run=False, force_test=False, now=now + 60)
    assert a2["should_send"] is False, a2
    assert a2["reason"].startswith("idempotent"), a2
    # 恢复：出现发送
    events_ok = events + [_ev(now - 10, "send_ok")]
    v_ok = nw.evaluate(events_ok, now=now + 120, window_sec=15 * 60, grace_sec=4 * 60)
    assert v_ok["is_dumb"] is False
    a3 = nw.apply_alert_policy(st2, v_ok, dry_run=False, force_test=False, now=now + 120)
    assert a3["clear_lock"] is True and a3["should_send"] is False
    st3 = nw.commit_state_after_action(st2, v_ok, a3, now=now + 120, dry_run=False)
    assert st3["alerting"] is False
    # 再次哑火可再告
    v4 = nw.evaluate([_ev(now + 120 - 300, "inbound")], now=now + 120, window_sec=15 * 60, grace_sec=4 * 60)
    a4 = nw.apply_alert_policy(st3, v4, dry_run=False, force_test=False, now=now + 120)
    assert a4["should_send"] is True and a4["reason"] == "new-mute"
    print("OK idempotent + recover")


def test_dry_run_no_lock():
    now = 1_000_000.0
    v = nw.evaluate([_ev(now - 300, "inbound")], now=now, window_sec=15 * 60, grace_sec=4 * 60)
    st = {"alerting": False, "last_fingerprint": ""}
    a = nw.apply_alert_policy(st, v, dry_run=True, force_test=False, now=now)
    # dry-run 仍给出 would-send 判定，但 commit 不锁
    assert a["should_send"] is True
    st2 = nw.commit_state_after_action(st, v, a, now=now, dry_run=True)
    assert st2.get("alerting") is False
    print("OK dry-run no lock")


def test_parse_ts():
    t = nw.parse_ts("10-31 12:00:00", 2026)
    assert t is not None
    assert nw.parse_ts("bad", 2026) is None
    print("OK parse_ts")


def main():
    test_classify()
    test_parse_ts()
    test_dumb_silence()
    test_healthy_inbound_reply_send()
    test_dumb_planner_no_reply()
    test_within_grace_not_dumb()
    test_idempotent_policy()
    test_dry_run_no_lock()
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
