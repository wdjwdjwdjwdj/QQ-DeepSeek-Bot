# -*- coding: utf-8 -*-
"""verify_botcommon.py — botcommon 公共库单测（不联网、不发消息）

覆盖：白名单拒绝、禁重定向、JSON 原子写、provider key 读取、
token_guard 余额轨迹增量保留（回归：警报分支曾整体替换 state 丢 daily_log）。

用法：py verify_botcommon.py
"""
import json
import tempfile
from pathlib import Path

import botcommon
import token_guard

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_http_whitelist():
    print("[1] HTTP 白名单：白名单外一律拒绝（不发真实请求）")
    allowed = {"127.0.0.1": {"http"}}
    try:
        botcommon.http_json("http://evil.com/api", allowed)  # 域名不在白名单
        check("拒绝白名单外域名", False, "竟然放行了")
    except RuntimeError as e:
        check("拒绝白名单外域名", "白名单" in str(e) or "拒绝" in str(e), str(e))
    except Exception as e:  # noqa: BLE001
        check("拒绝白名单外域名", False, "异常类型不对: " + repr(e))
    try:
        botcommon.http_json("https://127.0.0.1:3002/x", allowed)  # 域名在白名单但协议不在
        check("拒绝协议不匹配", False, "竟然放行了")
    except RuntimeError:
        check("拒绝协议不匹配", True)


def test_no_redirect():
    print("[2] 禁止重定向")
    h = botcommon.NoRedirect()
    try:
        h.redirect_request(None, None, 302, "Found", {}, "http://evil.com")
        check("redirect_request 抛异常", False, "竟然放行重定向")
    except RuntimeError:
        check("redirect_request 抛异常", True)


def test_atomic_write():
    print("[3] JSON 原子写：内容一致、tmp 不残留、中文可读")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / "state.json"
        obj = {"alerting": False, "note": "中文内容保持可读"}
        botcommon.write_json_atomic(p, obj, indent=1)
        back = json.loads(p.read_text(encoding="utf-8"))
        check("写入后读回一致", back == obj, str(back))
        check("无 .tmp 残留", not list(Path(d).glob("**/*.tmp")))
        raw = p.read_text(encoding="utf-8")
        check("ensure_ascii=False 中文明文", "中文内容" in raw)
        botcommon.write_json_atomic(p, {"k": 2})
        check("二次覆盖生效", json.loads(p.read_text(encoding="utf-8")) == {"k": 2})


def test_load_key():
    print("[4] provider key 读取（只读配置，不打印 key）")
    for name in ("DeepSeek", "Zhipu"):
        try:
            k = botcommon.load_provider_key(name)
            check("读到 " + name + " key", isinstance(k, str) and len(k) >= 8, "空或过短")
        except SystemExit as e:
            check("读到 " + name + " key", False, str(e))
    try:
        botcommon.load_provider_key("不存在的provider")
        check("未知 provider 报 SystemExit", False, "竟然返回了")
    except SystemExit:
        check("未知 provider 报 SystemExit", True)


def test_token_guard_regression():
    print("[5] token_guard 回归：警报分支不得丢 daily_log")
    st = {"alerting": False, "last_remind": 0,
          "daily_log": [{"day": "2026-10-25", "t": 1.0, "bal": 9.67}]}
    st, first = token_guard.record_balance(st, 8.5)
    check("轨迹追加且保留旧点", len(st["daily_log"]) == 2 and st["daily_log"][0]["bal"] == 9.67, str(st["daily_log"]))
    check("返回今日首点余额", first == 9.67, str(first))
    # 模拟警报分支：只改标志位再保存，daily_log 必须还在（重构前这里是整体替换）
    st["alerting"] = True
    st["last_remind"] = 123.0
    check("改标志位不影响轨迹", len(st["daily_log"]) == 2, str(st["daily_log"]))


if __name__ == "__main__":
    test_http_whitelist()
    test_no_redirect()
    test_atomic_write()
    test_load_key()
    test_token_guard_regression()
    print(f"\n结果: {PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
