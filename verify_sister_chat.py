# -*- coding: utf-8 -*-
"""校验姐的定时/主动私聊已被总开关关掉（不发 QQ、不出网）。

调查结论（本测试锁住的不变量）：
- 仓库里会主动私聊姐的计划任务只有 sister_chat.py，由 deploy/crontab.maibot 每 30 分钟调用。
- scheduled_sender.py 只发 schedule_config.json；该文件不进仓库。总开关关闭时，发给姐的私聊任务在 NapCat 之前跳过，群任务和其他私聊仍发。
- sister_chat_config.json enabled=false 时，常规轮询和 --force 都在 LLM / NapCat 之前返回。
- crontab 源文件里 sister_chat.py 没有生效行，重装 crontab 不会把任务加回去。
- 人设短锚里认姐规则仍在；本测试不改人设、不改群聊。

用法：python3 verify_sister_chat.py
网络边界：不出网、不发 QQ。开关打开的正向用例只打到注入的假发送函数，并猴子补丁掉真实发送。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import botcommon
import sister_chat

ROOT = Path(__file__).resolve().parent


def _boom(*_a, **_k):
    raise AssertionError("不应出网或调用 NapCat")


def _guard_network():
    saved = (
        botcommon.napcat_send_private,
        botcommon.llm_free_chat,
        botcommon.http_json,
    )
    botcommon.napcat_send_private = _boom
    botcommon.llm_free_chat = _boom
    botcommon.http_json = _boom
    return saved


def _restore_network(saved):
    (
        botcommon.napcat_send_private,
        botcommon.llm_free_chat,
        botcommon.http_json,
    ) = saved


def test_repo_switch_off():
    cfg = sister_chat.load_config()
    assert cfg["source"] == "file", cfg
    assert cfg["enabled"] is False, cfg
    status = sister_chat.probe_status()
    assert status["config_file"] == "sister_chat_config.json"
    assert status["enabled"] is False
    assert status["proactive_private_send"] == "off"
    assert status["force_overrides_switch"] is False
    assert status["blocks_before_llm"] is True
    assert status["blocks_before_napcat"] is True
    print("OK repo switch off", json.dumps(status, ensure_ascii=False))


def test_unrecognized_flag_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        missing = sister_chat.load_config(base / "missing.json")
        assert missing["enabled"] is False and missing["source"] == "missing"
        bad = base / "bad.json"
        bad.write_text("{", encoding="utf-8")
        invalid = sister_chat.load_config(bad)
        assert invalid["enabled"] is False and invalid["source"] == "invalid"
        (base / "list.json").write_text("[]", encoding="utf-8")
        assert sister_chat.load_config(base / "list.json")["enabled"] is False
        for raw in ({}, {"enabled": "false"}, {"enabled": "no"}, {"enabled": "off"}, {"enabled": 0}):
            path = base / "case.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            assert sister_chat.load_config(path)["enabled"] is False, raw
        path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        assert sister_chat.load_config(path)["enabled"] is True
        path.write_text(json.dumps({"enabled": "true"}), encoding="utf-8")
        assert sister_chat.load_config(path)["enabled"] is True
    print("OK fail-closed flag parse")


def test_disabled_blocks_even_force():
    state = sister_chat.STATE_FILE
    before = state.read_bytes() if state.exists() else None
    calls = {"llm": 0, "send": 0}

    def llm(_ctx):
        calls["llm"] += 1
        return "不该生成"

    def send(_text):
        calls["send"] += 1

    for argv in ([], ["--force"]):
        result = sister_chat.main(
            argv=argv,
            llm_fn=llm,
            send_private=send,
            roll=lambda: 0.0,
        )
        assert result["action"] == "skip" and result["reason"] == "disabled", result
        assert result["sent"] is False
    assert calls == {"llm": 0, "send": 0}, calls
    after = state.read_bytes() if state.exists() else None
    assert before == after
    print("OK disabled blocks poll and --force")


def test_switch_on_still_reaches_stub_send_only():
    """开关为 true 时功能还在：--force 会走到注入的发送函数，不会走到 NapCat。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg_path = base / "on.json"
        cfg_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        state_path = base / "state.json"
        sent = []

        def llm(_ctx):
            return "测试短句"

        def send(text):
            sent.append(text)

        result = sister_chat.main(
            argv=["--force"],
            llm_fn=llm,
            send_private=send,
            config_path=cfg_path,
            state_path=state_path,
            now_fn=lambda: 1000.0,
        )
        assert result["sent"] is True, result
        assert sent == ["测试短句"]
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["count"] == 1 and saved["last_sent"] == 1000.0

        sent.clear()
        skip_state = base / "skip-state.json"
        skipped = sister_chat.main(
            argv=["--force"],
            llm_fn=lambda _ctx: "[SKIP]",
            send_private=send,
            config_path=cfg_path,
            state_path=skip_state,
        )
        assert skipped["reason"] == "empty" and skipped["sent"] is False
        assert sent == []
        assert not skip_state.exists()

        rolled = sister_chat.main(
            argv=[],
            llm_fn=llm,
            send_private=send,
            config_path=cfg_path,
            state_path=base / "roll-state.json",
            roll=lambda: 0.99,
            now_fn=lambda: 9_000_000.0,
        )
        assert rolled["reason"] == "roll" and rolled["sent"] is False
        assert sent == []
    print("OK enabled path still gated, stub send only")


def test_crontab_job_inactive():
    text = (ROOT / "deploy" / "crontab.maibot").read_text(encoding="utf-8")
    active = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        active.append(stripped)
    sister_lines = [line for line in active if "sister_chat.py" in line]
    assert sister_lines == [], sister_lines
    assert any(
        "sister_chat.py" in line and line.strip().startswith("#")
        for line in text.splitlines()
    )
    assert any("scheduled_sender.py" in line for line in active)
    tracked = subprocess.check_output(
        ["git", "ls-files", "--", "schedule_config.json"],
        cwd=str(ROOT),
        text=True,
    ).strip()
    assert tracked == ""
    print("OK crontab sister line inactive; schedule_config.json untracked")


def test_scheduled_sender_skips_sister_private():
    import time as time_mod
    import scheduled_sender as ss

    sister_id = str(sister_chat.SISTER_QQ)
    fixed_hm = "12:34"
    tasks = [
        {
            "name": "sister-private",
            "enabled": True,
            "times": [fixed_hm],
            "target": {"type": "private", "id": int(sister_id)},
            "messages": ["不该发给姐"],
        },
        {
            "name": "sister-private-str",
            "enabled": True,
            "times": [fixed_hm],
            "target": {"type": "private", "id": sister_id},
            "messages": ["不该发给姐"],
        },
        {
            "name": "other-private",
            "enabled": True,
            "times": [fixed_hm],
            "target": {"type": "private", "id": 1},
            "messages": ["其他人可以"],
        },
        {
            "name": "group-ok",
            "enabled": True,
            "times": [fixed_hm],
            "target": {"type": "group", "id": 2},
            "messages": ["群照常"],
        },
    ]
    assert ss.sister_outbound_blocked(tasks[0]) is True
    assert ss.sister_outbound_blocked(tasks[1]) is True
    assert ss.sister_outbound_blocked(tasks[2]) is False
    assert ss.sister_outbound_blocked(tasks[3]) is False
    assert ss.sister_outbound_blocked(tasks[0], cfg={"enabled": True, "source": "file"}) is False

    orig_strftime = time_mod.strftime

    def fake_strftime(fmt, t=None):
        if fmt == "%H:%M":
            return fixed_hm
        if fmt == "%Y-%m-%d":
            return "2099-01-01"
        if fmt == "%w":
            return "1"
        return orig_strftime(fmt) if t is None else orig_strftime(fmt, t)

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg_path = base / "schedule_config.json"
        state_path = base / "scheduled_sender_state.json"
        cfg_path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
        sent = []

        def fake_send(task, text):
            sent.append(task["name"])

        old = (ss.CONFIG_FILE, ss.STATE_FILE, ss.send_one, ss.time.strftime, sister_chat.load_config)
        ss.CONFIG_FILE = cfg_path
        ss.STATE_FILE = state_path
        ss.send_one = fake_send
        ss.time.strftime = fake_strftime
        try:
            ss.main([])
            assert "sister-private" not in sent, sent
            assert "sister-private-str" not in sent, sent
            assert sent == ["other-private", "group-ok"], sent
            ss.main(["--test", "sister-private"])
            assert sent == ["other-private", "group-ok"], sent

            sent.clear()
            sister_chat.load_config = lambda path=None: {"enabled": True, "source": "file"}
            ss.main([])
            assert "sister-private" in sent and "sister-private-str" in sent, sent
        finally:
            ss.CONFIG_FILE, ss.STATE_FILE, ss.send_one, ss.time.strftime, sister_chat.load_config = old
    print("OK scheduled_sender skips sister private only")


def test_persona_sister_rule_kept():
    anchor = (ROOT / "deploy" / "apply_persona_anchor.py").read_text(encoding="utf-8")
    assert "认哥认姐只看号" in anchor
    assert "姐是家人不走关系账本" in anchor
    ledger = (ROOT / "my-maibot" / "plugins" / "FriendLedger" / "plugin.py").read_text(encoding="utf-8")
    assert "家人不走感情分" in ledger
    print("OK persona / ledger sister rules kept")


def test_cli_probe_and_force_do_not_send():
    probe = subprocess.run(
        [sys.executable, str(ROOT / "sister_chat.py"), "--probe"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    payload = json.loads(probe.stdout.strip())
    assert payload["enabled"] is False
    assert payload["proactive_private_send"] == "off"
    assert payload["blocks_before_napcat"] is True

    for args in ([], ["--force"]):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "sister_chat.py"), *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert proc.returncode == 0, (args, proc.returncode, proc.stdout, proc.stderr)
        assert "总开关关闭" in proc.stdout
        assert "SENT" not in proc.stdout
    print("OK cli --probe / poll / --force exit without send")
    print("PROBE " + json.dumps(payload, ensure_ascii=False))


def main():
    saved = _guard_network()
    try:
        test_repo_switch_off()
        test_unrecognized_flag_fails_closed()
        test_disabled_blocks_even_force()
        test_switch_on_still_reaches_stub_send_only()
        test_crontab_job_inactive()
        test_scheduled_sender_skips_sister_private()
        test_persona_sister_rule_kept()
    finally:
        _restore_network(saved)
    # 子进程走真实入口；总开关关闭时不应去连 NapCat。
    test_cli_probe_and_force_do_not_send()
    print("ALL SISTER CHAT SWITCH CHECKS PASSED")


if __name__ == "__main__":
    main()
