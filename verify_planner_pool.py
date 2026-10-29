# -*- coding: utf-8 -*-
"""校验 planner 池首位是已验证会 tool call 的模型。

调用：手工 / night_watch / 改 model_config 后冒烟；非 cron。
用法：
  python3 verify_planner_pool.py           # 只读本地 toml，不出网
  python3 verify_planner_pool.py --live    # 额外对 planner[0] 做 tool_call 冒烟（出网）

网络边界：默认不出网；--live 仅打 planner 首位所属 provider。

扩容 TOOL_CAPABLE_PLANNER_HEADS 规则（2026-10-30）：
1) 硬提示冒烟稳定出 tool_calls；2) 生产私聊实测 planner→reply→出话；
3) 再改本文件白名单。免费 flash 简单冒烟 PASS 不够——复杂 Planner 提示下会吐纯文本/假 JSON。
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG = BASE / "my-maibot/config/model_config.toml"

# 仅放生产已证明会稳定发 tool_calls 的 planner 名（model_config models[].name）
TOOL_CAPABLE_PLANNER_HEADS = frozenset({"deepseek-flash-planner"})

# 硬提示下曾把 tool JSON 当正文吐出、或生产静默过的模型——禁止当 planner 首位
KNOWN_UNRELIABLE_PLANNER_HEADS = frozenset({
    "zai-flash",
    "zp-4flash",
    "zp-4flash-b",
    "ds-v4-flash",
    "sn-ds",
    "zp-flash",
    "zp-flashx",
})


def _load() -> dict:
    if not CFG.is_file():
        raise FileNotFoundError(f"MISSING {CFG}")
    return tomllib.loads(CFG.read_text(encoding="utf-8"))


def check_static(data: dict) -> list[str]:
    """返回问题列表；空列表=OK。"""
    problems: list[str] = []
    models = data["model_task_config"]["planner"]["model_list"]
    if not models:
        problems.append("planner.model_list empty")
        return problems
    head = models[0]
    if head not in TOOL_CAPABLE_PLANNER_HEADS:
        problems.append(
            f"planner[0]={head!r} not in TOOL_CAPABLE_PLANNER_HEADS={sorted(TOOL_CAPABLE_PLANNER_HEADS)}"
        )
    if head in KNOWN_UNRELIABLE_PLANNER_HEADS:
        problems.append(
            f"planner[0]={head!r} is known-unreliable as planner head (free flash silent / fake JSON)"
        )
    if not any(m in TOOL_CAPABLE_PLANNER_HEADS for m in models):
        problems.append("planner pool has no tool-capable head candidate at all")
    # replyer 允许免费优先，但建议池内仍有 DeepSeek 兜底（弱警告，不阻断）
    return problems


def check_live(data: dict) -> list[str]:
    """对 planner[0] 发 MaiBot 风格 tools 请求，必须出现 tool_calls。"""
    from botcommon import http_json, load_provider_key

    problems: list[str] = []
    head = data["model_task_config"]["planner"]["model_list"][0]
    by_name = {m["name"]: m for m in data["models"]}
    providers = {p["name"]: p for p in data["api_providers"]}
    m = by_name.get(head)
    if not m:
        return [f"planner head {head!r} missing from models[]"]
    prov = providers[m["api_provider"]]
    url = prov["base_url"].rstrip("/") + "/chat/completions"
    key = load_provider_key(m["api_provider"])
    host = {
        "DeepSeek": "api.deepseek.com",
        "ZAI": "api.z.ai",
        "Zhipu": "open.bigmodel.cn",
        "SenseNova": "token.sensenova.cn",
        "TencentMaaS": "tokenhub.tencentmaas.com",
    }.get(m["api_provider"])
    if not host:
        return [f"no allowlist host for provider {m['api_provider']!r}"]
    allowed = {host: {"https"}}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "reply",
                "description": "向当前聊天发送一条文本回复。需要回复用户时必须调用。",
                "parameters": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "no_reply",
                "description": "本轮决定不回复。",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                },
            },
        },
    ]
    system = (
        "你是 QQ 机器人「大肥鱼」的 Planner。你必须通过调用工具来行动，禁止只输出分析文字。"
        "可用工具：reply（回复）、no_reply（不回）。"
        "不要用自然语言描述「我将调用 reply」，直接发出 tool_calls。"
    )
    user = "[私聊] 用户「哥」说：在吗\n请规划本轮行动。"
    body = {
        "model": m["model_identifier"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    if m["api_provider"] in ("ZAI", "DeepSeek"):
        body["thinking"] = {"type": "disabled"}
    try:
        resp = http_json(
            url,
            allowed,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
            timeout=90,
        )
    except Exception as e:
        err = str(e)[:160].replace("\n", " ")
        if key in err:
            err = err.replace(key, "***")
        return [f"live smoke ERROR for {head}: {err}"]
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls")
    if not tcs:
        content = (msg.get("content") or "")[:160].replace("\n", " ")
        problems.append(
            f"live smoke FAIL {head}: no tool_calls finish={choice.get('finish_reason')} content={content!r}"
        )
    return problems


def main() -> int:
    live = "--live" in sys.argv[1:]
    try:
        data = _load()
    except Exception as e:
        print(f"FAIL: {e}")
        return 2
    models = data["model_task_config"]["planner"]["model_list"]
    print("planner.model_list =", models)
    problems = check_static(data)
    if live:
        problems.extend(check_live(data))
    if problems:
        for p in problems:
            print("FAIL:", p)
        return 1
    msg = "OK: planner head is tool-capable"
    if live:
        msg += " (live tool_call PASS)"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
