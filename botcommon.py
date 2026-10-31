# -*- coding: utf-8 -*-
"""botcommon.py — 根目录计划任务脚本公共库

watchdog / token_guard / scheduled_sender / cognition / persona_audit 共用的
基础设施：白名单 HTTP、provider key 读取、JSON 原子写、NapCat 私聊发送。
业务脚本只保留各自的业务逻辑，网络与落盘约定统一在这里维护。

约定（详见项目根 AGENTS.md）：
- 出网必须走 http_json 且显式传 allowed 白名单，白名单外一律拒绝；
- 一律禁止跟随重定向；
- API key 只从 my-maibot/config/model_config.toml 读取，不硬编码、不进新文件；
- 状态文件一律 write_json_atomic 落盘（临时文件 + os.replace）。
"""
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
MODEL_CONFIG = BASE / "my-maibot" / "config" / "model_config.toml"
NAPCAT_API = "http://127.0.0.1:3002/send_private_msg"
NAPCAT_GROUP_API = "http://127.0.0.1:3002/send_group_msg"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("禁止重定向: " + newurl)


def http_json(url, allowed, data=None, headers=None, timeout=30):
    """白名单内的 HTTP JSON 请求。allowed 形如 {"127.0.0.1": {"http"}}；
    域名或协议不在白名单内抛 RuntimeError；不跟随重定向。"""
    parts = urlsplit(url)
    allowed_schemes = allowed.get(parts.hostname or "")
    if allowed_schemes is None or parts.scheme not in allowed_schemes:
        raise RuntimeError("目标不在白名单内，拒绝请求: " + url)
    req = urllib.request.Request(url, data=data, headers=headers or {})
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=timeout) as r:
        return json.load(r)


def load_provider_key(provider_name):
    import tomllib
    with open(MODEL_CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    for p in cfg.get("api_providers", []):
        if p.get("name") == provider_name:
            return p["api_key"]
    raise SystemExit("model_config.toml 中未找到 provider: " + provider_name)


def write_json_atomic(path, obj, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(tmp, path)


def http_bytes(url, allowed, timeout=60):
    """白名单内的 HTTP 二进制下载（如生图结果）。校验规则同 http_json，不跟随重定向。"""
    parts = urlsplit(url)
    allowed_schemes = allowed.get(parts.hostname or "")
    if allowed_schemes is None or parts.scheme not in allowed_schemes:
        raise RuntimeError("目标不在白名单内，拒绝请求: " + url)
    req = urllib.request.Request(url)
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=timeout) as r:
        return r.read()



ALLOWED_LLM_FREE = {
    "token.sensenova.cn": {"https"},
    "open.bigmodel.cn": {"https"},
    "api.z.ai": {"https"},
    "openrouter.ai": {"https"},
    "tokenhub.tencentmaas.com": {"https"},
}
ALLOWED_LLM_PAID = {
    "api.deepseek.com": {"https"},
}

# MaiBot 池内别名 tx-hy3；TokenHub Chat Completions 的 model 参数是 hy3。
# 生产 toml 若把 identifier 配成别的值，llm_free_chat 以 toml 为准。
TOKENHUB_POOL_ALIAS = "tx-hy3"
TOKENHUB_CHAT_MODEL = "hy3"
TOKENHUB_CHAT_URL = "https://tokenhub.tencentmaas.com/v1/chat/completions"

# 免费优先级联：Z.AI → 智谱永久免费 Flash → OpenRouter 免费 → TokenHub tx-hy3 → 商汤
# 付费 DeepSeek 默认关闭（cron 勿烧鱼粮）。tx-hy3 不进 planner 首位。
# 注：glm-5.3-flash 需资源包(1113)；glm-4-flash / glm-4-flash-250414 为平台永久免费层
FREE_LLM_CASCADE = [
    ("ZAI", "https://api.z.ai/api/paas/v4/chat/completions", "glm-4.7-flash", {"thinking": {"type": "disabled"}}),
    ("Zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4-flash-250414", None),
    ("Zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4-flash", None),
    ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions", "openrouter/free", None),
    ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions", "poolside/laguna-s-2.1:free", None),
    ("TencentMaaS", TOKENHUB_CHAT_URL, TOKENHUB_CHAT_MODEL, None),
    ("SenseNova", "https://token.sensenova.cn/v1/chat/completions", "deepseek-v4-flash", None),
    ("SenseNova", "https://token.sensenova.cn/v1/chat/completions", "deepseek-flash", None),
]
PAID_LLM_CASCADE = [
    ("DeepSeek", "https://api.deepseek.com/chat/completions", "deepseek-flash", {"thinking": {"type": "disabled"}}),
]


def tokenhub_chat_model(default=None):
    """toml 里 name=tx-hy3 的 model_identifier；没有配置文件时用 hy3。"""
    default = TOKENHUB_CHAT_MODEL if default is None else default
    if not MODEL_CONFIG.is_file():
        return default
    try:
        import tomllib
        with open(MODEL_CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        print("tokenhub_chat_model 读配置失败，用", default, ":", type(e).__name__)
        return default
    for m in cfg.get("models") or []:
        if m.get("name") == TOKENHUB_POOL_ALIAS:
            ident = str(m.get("model_identifier") or "").strip()
            return ident or default
    return default


def llm_free_chat(prompt, temperature=0.3, timeout=90, allow_paid=False, max_tokens=None):
    """免费优先 chat completions。默认 allow_paid=False，全免费失败则抛错不烧 DeepSeek。

    返回 (text, provider, model)。调用方按需解析 JSON / 校验格式。
    """
    attempts = list(FREE_LLM_CASCADE)
    if allow_paid:
        if is_paid_budget_locked():
            print("llm_free_chat: 日预算已锁，忽略 allow_paid")
        else:
            attempts.extend(PAID_LLM_CASCADE)
    last_err = None
    for provider, url, model, extra in attempts:
        use_model = tokenhub_chat_model(model) if provider == "TencentMaaS" else model
        body_obj = {
            "model": use_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if max_tokens is not None:
            body_obj["max_tokens"] = max_tokens
        if extra:
            body_obj.update(extra)
        allowed = dict(ALLOWED_LLM_FREE)
        if provider == "DeepSeek":
            allowed = dict(ALLOWED_LLM_PAID)
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + load_provider_key(provider),
        }
        if provider == "OpenRouter":
            headers["HTTP-Referer"] = "https://github.com/wdjwdjwdjwdj/QQ-DeepSeek-Bot"
            headers["X-Title"] = "MaiBot-DaFeiYu"
        try:
            res = http_json(
                url,
                allowed,
                data=json.dumps(body_obj).encode("utf-8"),
                headers=headers,
                timeout=timeout,
            )
            msg = res["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            # OpenRouter 免费层偶发只有 reasoning、content 为空：按空 content 失败并级联
            if provider == "OpenRouter":
                text = content
            else:
                text = content or (msg.get("reasoning_content") or "").strip()
            if not text:
                raise RuntimeError("empty content from %s/%s" % (provider, use_model))
            return text, provider, use_model
        except Exception as e:
            last_err = e
            print("llm_free_chat 失败 %s/%s: %s" % (provider, use_model, str(e)[:120]))
    raise RuntimeError("llm_free_chat 全通道失败: " + str(last_err)[:160])



BUDGET_CONFIG = BASE / "budget_config.json"
TOKEN_GUARD_STATE = BASE / "my-maibot" / "data" / "token_guard_state.json"

# 默认：日预算 ¥1；超预算后主聊/工具池踢付费，planner 仍可 DeepSeek（tool_call）
_DEFAULT_BUDGET = {
    "enabled": True,
    "daily_cny": 1.0,
    "keep_planner_paid": True,
    "gated_tasks": ["replyer", "utils", "fast_model"],
    "paid_models": ["deepseek-flash-reply", "deepseek-flash"],
    "notify": True,
    "restart_on_change": True,
}


def load_budget_config():
    """读根目录 budget_config.json；缺文件/坏 JSON 用默认。不含密钥。"""
    cfg = dict(_DEFAULT_BUDGET)
    if BUDGET_CONFIG.is_file():
        try:
            raw = json.loads(BUDGET_CONFIG.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception as e:
            print("load_budget_config 失败，用默认:", e)
    cfg["daily_cny"] = float(cfg.get("daily_cny", 1.0))
    cfg["gated_tasks"] = list(cfg.get("gated_tasks") or _DEFAULT_BUDGET["gated_tasks"])
    cfg["paid_models"] = list(cfg.get("paid_models") or _DEFAULT_BUDGET["paid_models"])
    return cfg


def today_deepseek_spend(daily_log, current_balance=None):
    """当日 DeepSeek 消耗（元）= 当天首点余额 − 当前余额；负值（充值）按 0。

    返回 (spend, first_bal_or_None)。daily_log 形如 token_guard 的 [{day,t,bal},...]。
    """
    import time as _time
    today = _time.strftime("%Y-%m-%d")
    pts = [e for e in (daily_log or []) if e.get("day") == today]
    if not pts:
        return 0.0, None
    first = float(pts[0]["bal"])
    last = float(current_balance) if current_balance is not None else float(pts[-1]["bal"])
    return max(0.0, first - last), first


def is_paid_budget_locked(daily_log=None, current_balance=None, cfg=None):
    """超日预算则 True。enabled=False 永远 False。"""
    cfg = cfg or load_budget_config()
    if not cfg.get("enabled", True):
        return False
    if daily_log is None:
        if TOKEN_GUARD_STATE.is_file():
            try:
                daily_log = json.loads(
                    TOKEN_GUARD_STATE.read_text(encoding="utf-8")
                ).get("daily_log", [])
            except Exception:
                daily_log = []
        else:
            daily_log = []
    spend, _ = today_deepseek_spend(daily_log, current_balance)
    return spend >= float(cfg["daily_cny"])


def strip_paid_from_list(model_list, paid_models):
    paid = set(paid_models)
    return [m for m in model_list if m not in paid]


def restore_paid_into_list(model_list, paid_to_add, paid_models):
    """在列表上恢复付费模型：sn-vlm 等视觉模型仍置尾。"""
    paid = set(paid_models)
    base = [m for m in model_list if m not in paid]
    trail_vlm = [m for m in base if "vlm" in m.lower() or m.endswith("-vlm")]
    head = [m for m in base if m not in trail_vlm]
    add = [m for m in paid_to_add if m in paid and m not in head]
    return head + add + trail_vlm


def napcat_send_private(user_id, text):
    """通过本机 NapCat HTTP API 发私聊；返回值 status 非 ok 时抛 RuntimeError。"""
    body = json.dumps({"user_id": user_id, "message": text}).encode("utf-8")
    res = http_json(
        NAPCAT_API,
        {"127.0.0.1": {"http"}},
        data=body,
        headers={"Content-Type": "application/json"},
    )
    if res.get("status") != "ok":
        raise RuntimeError("NapCat 发送失败: " + json.dumps(res, ensure_ascii=False)[:200])
    return res


def napcat_send_record(audio_b64, user_id=None, group_id=None):
    """通过本机 NapCat HTTP API 发语音 record 段；私聊或群聊二选一。

    audio_b64: 不含 base64:// 前缀的纯 base64 字符串（mp3/wav 均可，冒烟已验 mp3）。
    返回值 status 非 ok 时抛 RuntimeError。
    """
    if bool(user_id) == bool(group_id):
        raise ValueError("napcat_send_record 需要且只能指定 user_id 或 group_id 之一")
    msg = [{"type": "record", "data": {"file": "base64://" + audio_b64}}]
    if group_id:
        url = NAPCAT_GROUP_API
        body = {"group_id": int(group_id), "message": msg}
    else:
        url = NAPCAT_API
        body = {"user_id": int(user_id), "message": msg}
    res = http_json(
        url,
        {"127.0.0.1": {"http"}},
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    if res.get("status") != "ok":
        raise RuntimeError("NapCat 语音发送失败: " + json.dumps(res, ensure_ascii=False)[:200])
    return res
