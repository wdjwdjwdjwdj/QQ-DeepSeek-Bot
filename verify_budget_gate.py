# -*- coding: utf-8 -*-
"""校验付费日预算门控纯函数 + model_list 改写。

调用：手工 / 改 budget 后冒烟；非 cron。
用法：
  python3 verify_budget_gate.py           # 纯本地单测，不出网不改生产 toml
  python3 verify_budget_gate.py --dry-run # 对真实 model_config 算一遍将改内容（不写盘）

网络边界：默认不出网；不发 QQ。
"""
from __future__ import annotations

import sys
import tomllib

import botcommon
import token_guard as tg


def test_spend():
    log = [
        {"day": "2099-01-01", "t": 1, "bal": 10.0},
        {"day": "2099-01-01", "t": 2, "bal": 9.2},
    ]
    import time as _t
    real = _t.strftime

    def fake(fmt, t=None):
        if fmt == "%Y-%m-%d":
            return "2099-01-01"
        return real(fmt, t)

    _t.strftime = fake
    try:
        spend, first = botcommon.today_deepseek_spend(log, 9.0)
        assert abs(spend - 1.0) < 1e-9 and first == 10.0, (spend, first)
        spend2, _ = botcommon.today_deepseek_spend(log, 11.0)
        assert spend2 == 0.0, spend2
        locked = botcommon.is_paid_budget_locked(log, 9.0, {"enabled": True, "daily_cny": 1.0})
        assert locked is True
        unlocked = botcommon.is_paid_budget_locked(log, 9.5, {"enabled": True, "daily_cny": 1.0})
        assert unlocked is False
        off = botcommon.is_paid_budget_locked(log, 9.0, {"enabled": False, "daily_cny": 1.0})
        assert off is False
    finally:
        _t.strftime = real
    print("OK spend/lock math")


def test_list_ops():
    paid = ["deepseek-flash-reply", "deepseek-flash"]
    lst = ["zai-flash", "zp-4flash", "deepseek-flash-reply", "sn-vlm"]
    stripped = botcommon.strip_paid_from_list(lst, paid)
    assert stripped == ["zai-flash", "zp-4flash", "sn-vlm"], stripped
    restored = botcommon.restore_paid_into_list(stripped, ["deepseek-flash-reply"], paid)
    assert restored == ["zai-flash", "zp-4flash", "deepseek-flash-reply", "sn-vlm"], restored
    print("OK list strip/restore")


def test_toml_rewrite():
    sample = """
[model_task_config.replyer]
name = "replyer"
model_list = ["zai-flash", "deepseek-flash-reply"]
max_tokens = 2048

[model_task_config.planner]
name = "planner"
model_list = ["deepseek-flash-planner", "zai-flash"]
max_tokens = 4096

[model_task_config.utils]
name = "utils"
model_list = ["zai-flash", "deepseek-flash-reply", "sn-vlm"]
"""
    new = tg.set_task_model_lists(
        sample,
        {
            "replyer": ["zai-flash"],
            "utils": ["zai-flash", "sn-vlm"],
        },
    )
    cfg = tomllib.loads(new)
    assert cfg["model_task_config"]["replyer"]["model_list"] == ["zai-flash"]
    assert cfg["model_task_config"]["utils"]["model_list"] == ["zai-flash", "sn-vlm"]
    assert cfg["model_task_config"]["planner"]["model_list"] == [
        "deepseek-flash-planner",
        "zai-flash",
    ]
    new2 = tg.set_task_model_lists(
        new,
        {
            "replyer": ["zai-flash", "deepseek-flash-reply"],
            "utils": ["zai-flash", "deepseek-flash-reply", "sn-vlm"],
        },
    )
    cfg2 = tomllib.loads(new2)
    assert "deepseek-flash-reply" in cfg2["model_task_config"]["replyer"]["model_list"]
    print("OK toml rewrite (planner untouched)")


def test_compute_gated():
    bcfg = {
        "gated_tasks": ["replyer", "utils", "fast_model"],
        "paid_models": ["deepseek-flash-reply", "deepseek-flash"],
    }
    current = {
        "replyer": ["zai-flash", "deepseek-flash-reply"],
        "utils": ["zai-flash", "deepseek-flash-reply", "sn-vlm"],
        "fast_model": ["zai-flash", "deepseek-flash-reply"],
        "planner": ["deepseek-flash-planner", "zai-flash"],
    }
    locked = tg.compute_gated_lists(current, bcfg, locked=True)
    assert locked["replyer"] == ["zai-flash"]
    assert locked["utils"] == ["zai-flash", "sn-vlm"]
    assert "planner" not in locked
    unlocked = tg.compute_gated_lists(locked, bcfg, locked=False)
    assert "deepseek-flash-reply" in unlocked["replyer"]
    assert unlocked["utils"][-1] == "sn-vlm"
    print("OK compute_gated_lists")


def dry_run_live():
    st = tg.load_state()
    log = st.get("daily_log") or []
    bal = log[-1]["bal"] if log else 0.0
    bcfg = botcommon.load_budget_config()
    spend, _ = botcommon.today_deepseek_spend(log, bal)
    print("live dry-run: spend=¥%.2f budget=¥%.2f" % (spend, bcfg["daily_cny"]))
    raw = tg.MODEL_CFG.read_text(encoding="utf-8")
    cfg = tomllib.loads(raw)
    current = tg.read_task_lists(cfg)
    want_lock = bool(bcfg.get("enabled", True)) and spend >= float(bcfg["daily_cny"])
    target = tg.compute_gated_lists(current, bcfg, locked=want_lock)
    for task, lst in target.items():
        if current.get(task) != lst:
            print("WOULD %s: %s -> %s" % (task, current.get(task), lst))
        else:
            print("KEEP  %s: %s" % (task, lst))
    print("planner KEEP", current.get("planner"))
    tg.apply_model_lists(target, dry_run=True)
    print("OK live dry-run")


def main() -> int:
    dry = "--dry-run" in sys.argv[1:]
    try:
        test_spend()
        test_list_ops()
        test_toml_rewrite()
        test_compute_gated()
        cfg = botcommon.load_budget_config()
        assert "daily_cny" in cfg
        print("OK load_budget_config", {k: cfg[k] for k in ("enabled", "daily_cny", "gated_tasks")})
        if dry:
            dry_run_live()
    except Exception as e:
        print("FAIL:", e)
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
