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

# 免费优先级联（2026-11-06）：智谱稳定层在前，Z.AI 让位（互动与 cron 不再抢同一热配额）。
# 尾部 OpenRouter / TokenHub / 商汤保留作保险，不删除；失败后按 TAIL_PROBE_INTERVAL_SECONDS 降频。
# 付费 DeepSeek 不在此表。tx-hy3 不进 planner 首位。
# 注：glm-5.3-flash 需资源包(1113)；glm-4-flash / glm-4-flash-250414 为平台永久免费层
FREE_LLM_CASCADE = [
    ("Zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4-flash-250414", None),
    ("Zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4-flash", None),
    ("ZAI", "https://api.z.ai/api/paas/v4/chat/completions", "glm-4.7-flash", {"thinking": {"type": "disabled"}}),
    ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions", "openrouter/free", None),
    ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions", "poolside/laguna-s-2.1:free", None),
    ("TencentMaaS", TOKENHUB_CHAT_URL, TOKENHUB_CHAT_MODEL, None),
    ("SenseNova", "https://token.sensenova.cn/v1/chat/completions", "deepseek-v4-flash", None),
    ("SenseNova", "https://token.sensenova.cn/v1/chat/completions", "deepseek-flash", None),
]
PAID_LLM_CASCADE = [
    ("DeepSeek", "https://api.deepseek.com/chat/completions", "deepseek-flash", {"thinking": {"type": "disabled"}}),
]

# 一次 429 即熔断，与 08 文档里 MaiBot 熔断冷却同量级；脚本侧让出 Z.AI 配额。
ZAI_COOLDOWN_SECONDS = 600
# 互动池里 Z.AI provider 的重试收紧：429 不要在同一模型上空转，sequential 尽快换到智谱。
ZAI_PROVIDER_MAX_RETRY = 1
ZAI_PROVIDER_RETRY_INTERVAL = 1
# 头层全失败后才打到尾部；打过一次后这段时间内不再盲探。
TAIL_PROBE_INTERVAL_SECONDS = 1800
TAIL_CASCADE_PROVIDERS = frozenset({"OpenRouter", "TencentMaaS", "SenseNova"})
CASCADE_CIRCUIT_STATE = BASE / "my-maibot" / "data" / "llm_cascade_circuit_state.json"

# 不传 tools 的旁路任务（MaiBot 源码核对，2026-11）：
# emoji：emoji_manager.replace_an_emoji_by_llm / send_emoji 子代理默认 tool_definitions=[]。
#   model_list 为空时替换决策 task_name 落到 planner，记账打在 deepseek-flash-planner。
# learner：expression_learner / expression_utils / jargon_* 只 generate_response，不传 tools。
#   空列表会继用 utils（含付费尾）。
# 不在此列：planner、compaction、reply_supplement（继承 planner 或要工具）、
# voice（ASR）、vlm、embedding、memory。
NON_TOOL_AUX_TASKS = ("emoji", "learner")
# 与现网 replyer/utils/fast 免费段同名，智谱在 Z.AI 前；不含 deepseek-flash-planner / 付费回复。
FREE_AUX_MODEL_ORDER = (
    "zp-4flash",
    "zp-4flash-b",
    "zai-flash",
    "or-free",
    "or-laguna",
    "ds-v4-flash",
    "sn-ds",
    "tx-hy3",
)
AUX_FORBIDDEN_MODELS = frozenset({
    "deepseek-flash-planner",
    "deepseek-flash-reply",
    "deepseek-flash",
    "balance",
})
# 只调整这几个已经是 sequential 的对话池里 Z.AI 与智谱的相对顺序，不动 planner。
CHAT_POOLS_YIELD_ZAI = ("replyer", "utils", "fast_model", "fast")
ZHIPU_POOL_NAMES = frozenset({"zp-4flash", "zp-4flash-b", "zp-flash", "zp-flashx"})
ZAI_POOL_NAMES = frozenset({"zai-flash"})


def free_aux_model_list(available_names):
    """按固定顺序取出配置里真实存在的免费旁路模型。不含 DeepSeek / 字面量 balance。"""
    available = {str(name) for name in (available_names or [])}
    return [name for name in FREE_AUX_MODEL_ORDER if name in available]


def move_zai_after_zhipu(model_list):
    """把 zai-flash 挪到本列表里最后一个智谱模型之后。没有智谱或没有 Z.AI 时原样返回。

    其余名字的相对顺序保持不变，付费尾和 vlm 不会被提前。
    """
    models = list(model_list or [])
    zp_idx = [i for i, name in enumerate(models) if name in ZHIPU_POOL_NAMES]
    zai_idx = [i for i, name in enumerate(models) if name in ZAI_POOL_NAMES]
    if not zp_idx or not zai_idx:
        return models
    if min(zai_idx) > max(zp_idx):
        return models
    zai_set = set(zai_idx)
    without = [name for i, name in enumerate(models) if i not in zai_set]
    last_zp = max(i for i, name in enumerate(without) if name in ZHIPU_POOL_NAMES)
    zai_models = [models[i] for i in zai_idx]
    return without[: last_zp + 1] + zai_models + without[last_zp + 1 :]


def cascade_text_ok(text):
    """可核验的正文门：空、纯空白、纯标点不算成功。单字「嗯」「好」和 ok 放行。"""
    if text is None:
        return False
    stripped = str(text).strip()
    if not stripped:
        return False
    # isalnum 覆盖汉字与字母数字；纯下划线/省略号没有可读字符，不算成功。
    return any(ch.isalnum() for ch in stripped)


def cascade_result_ok(text, tool_calls=None, require_tool_call=False):
    """成功判定。require_tool_call 时只认非空 tool_calls，不把「我应该调用工具」的正文当成功。

    默认不要求 tool_call：MaiBot planner 的等待/停止可以没有工具调用，本函数不替那条路径做推断。
    """
    if require_tool_call:
        return isinstance(tool_calls, (list, tuple)) and len(tool_calls) > 0
    return cascade_text_ok(text)


def is_rate_limit_error(exc):
    """只认 429。不把正文里偶然出现的数字 429 当成限流。"""
    for attr in ("code", "status", "status_code"):
        value = getattr(exc, attr, None)
        try:
            if int(value) == 429:
                return True
        except (TypeError, ValueError):
            pass
    message = str(exc)
    markers = (
        "HTTP Error 429",
        "Error code: 429",
        "status code 429",
        "status_code=429",
        "status_code: 429",
    )
    return any(marker in message for marker in markers)


def load_cascade_circuit(path=None):
    """读熔断状态。缺文件或坏 JSON 当空表，不抛给调用方。"""
    path = Path(path) if path else CASCADE_CIRCUIT_STATE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print("cascade circuit 读失败，当空:", type(e).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def save_cascade_circuit(state, path=None):
    """增量落盘：调用方应先读旧表再改字段，这里整表原子写回。"""
    path = Path(path) if path else CASCADE_CIRCUIT_STATE
    write_json_atomic(path, state if isinstance(state, dict) else {})


def plan_free_attempts(attempts, state, now):
    """按熔断与尾部探测间隔过滤本次要打的级联。不修改 state。"""
    state = state or {}
    try:
        zai_until = float(state.get("zai_open_until") or 0)
    except (TypeError, ValueError):
        zai_until = 0
    zai_blocked = zai_until > float(now)
    try:
        tail_at = float(state.get("tail_probed_at") or 0)
    except (TypeError, ValueError):
        tail_at = 0
    tails_due = (float(now) - tail_at) >= TAIL_PROBE_INTERVAL_SECONDS
    planned = []
    for item in attempts:
        provider = item[0]
        if provider == "ZAI" and zai_blocked:
            continue
        if provider in TAIL_CASCADE_PROVIDERS and not tails_due:
            continue
        planned.append(item)
    return planned


def note_zai_429(state, now):
    """一次 429 打开 Z.AI 熔断。保留 state 里的其他字段。"""
    updated = dict(state or {})
    updated["zai_open_until"] = float(now) + ZAI_COOLDOWN_SECONDS
    updated["zai_reason"] = "429"
    return updated


def note_zai_success(state):
    """Z.AI 成功则清熔断，其他字段保留。"""
    updated = dict(state or {})
    updated["zai_open_until"] = 0
    updated.pop("zai_reason", None)
    return updated


def note_tail_probe(state, now):
    """记录尾部已探测，窗口内不再盲探。其他字段保留。"""
    updated = dict(state or {})
    updated["tail_probed_at"] = float(now)
    return updated


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


def llm_free_chat(
    prompt,
    temperature=0.3,
    timeout=90,
    allow_paid=False,
    max_tokens=None,
    require_tool_call=False,
    circuit_state=None,
    now=None,
):
    """免费优先 chat completions。默认 allow_paid=False，全免费失败则抛错不烧 DeepSeek。

    空正文、纯标点不算成功，继续下一层。require_tool_call=True 时没有 tool_calls 也不算成功
    （默认 False：cron 脚本要的是正文，不在这里推断 planner 是否该调工具）。
    Z.AI 遇到 429 立刻熔断一段时间；OpenRouter / TokenHub / 商汤仍在表里，但探测过一次后降频。

    返回 (text, provider, model)。调用方按需解析 JSON / 校验格式。
    circuit_state 仅给测试注入；生产路径自己读盘，并在返回前把熔断增量写回。
    """
    import time as _time

    attempts = list(FREE_LLM_CASCADE)
    if allow_paid:
        if is_paid_budget_locked():
            print("llm_free_chat: 日预算已锁，忽略 allow_paid")
        else:
            attempts.extend(PAID_LLM_CASCADE)
    own_state = circuit_state is None
    state = load_cascade_circuit() if own_state else dict(circuit_state)
    now = _time.time() if now is None else float(now)
    planned = plan_free_attempts(attempts, state, now)
    if len(planned) != len(attempts):
        skipped = ["%s/%s" % (provider, model) for provider, _url, model, _extra in attempts if (provider, model) not in {
            (item[0], item[2]) for item in planned
        }]
        print("llm_free_chat 跳过:", ", ".join(skipped))
    last_err = None
    tail_used = False

    def _persist():
        if own_state:
            try:
                save_cascade_circuit(state)
            except Exception as e:
                print("cascade circuit 落盘失败:", type(e).__name__)

    for provider, url, model, extra in planned:
        if provider in TAIL_CASCADE_PROVIDERS:
            tail_used = True
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
            # 只认 content。reasoning 独白（包括「应使用某工具」）不是可核验成功。
            content = (msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls") or []
            if not cascade_result_ok(content, tool_calls, require_tool_call=require_tool_call):
                why = "missing tool_call" if require_tool_call else "empty or punctuation-only content"
                raise RuntimeError("quality gate %s from %s/%s" % (why, provider, use_model))
            text = content
            if require_tool_call and not text:
                text = json.dumps(tool_calls, ensure_ascii=False)
            if provider == "ZAI":
                state = note_zai_success(state)
            if tail_used:
                state = note_tail_probe(state, now)
            _persist()
            return text, provider, use_model
        except Exception as e:
            last_err = e
            if provider == "ZAI" and is_rate_limit_error(e):
                state = note_zai_429(state, now)
                _persist()
            print("llm_free_chat 失败 %s/%s: %s" % (provider, use_model, str(e)[:120]))
    if tail_used:
        state = note_tail_probe(state, now)
        _persist()
    elif own_state and state != load_cascade_circuit():
        _persist()
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
