# -*- coding: utf-8 -*-
"""校验免费池路由：emoji/learner 不走 DeepSeek，空响应不算成功，ZAI 429 让位，尾部仍在。

用法：python3 verify_free_routing.py
网络边界：不出网、不发 QQ、不读生产密钥。
"""
import importlib.util
import inspect
import json
import tomllib
from pathlib import Path

import botcommon

ROOT = Path(__file__).resolve().parent
PASS = 0
FAIL = 0

FIXTURE = """
[[api_providers]]
name = "ZAI"
base_url = "https://api.z.ai/api/paas/v4"
api_key = "not-a-real-key"
max_retry = 5
retry_interval = 10

[[api_providers]]
name = "Zhipu"
base_url = "https://open.bigmodel.cn/api/paas/v4"
api_key = "also-not-real"
max_retry = 2
retry_interval = 5

[[models]]
name = "deepseek-flash-planner"
model_identifier = "deepseek-v4-flash"
api_provider = "DeepSeek"

[[models]]
name = "zai-flash"
model_identifier = "glm-4.7-flash"
api_provider = "ZAI"

[[models]]
name = "zp-4flash"
model_identifier = "glm-4-flash-250414"
api_provider = "Zhipu"

[[models]]
name = "zp-4flash-b"
model_identifier = "glm-4-flash"
api_provider = "Zhipu"

[[models]]
name = "or-free"
model_identifier = "openrouter/free"
api_provider = "OpenRouter"

[[models]]
name = "or-laguna"
model_identifier = "poolside/laguna-s-2.1:free"
api_provider = "OpenRouter"

[[models]]
name = "ds-v4-flash"
model_identifier = "deepseek-v4-flash"
api_provider = "SenseNova"

[[models]]
name = "sn-ds"
model_identifier = "deepseek-flash"
api_provider = "SenseNova"

[[models]]
name = "tx-hy3"
model_identifier = "hy3"
api_provider = "TencentMaaS"

[[models]]
name = "deepseek-flash-reply"
model_identifier = "deepseek-v4-flash"
api_provider = "DeepSeek"

[[models]]
name = "sn-vlm"
model_identifier = "sensenova-u1-vlm"
api_provider = "SenseNova"

[[models]]
name = "balance"
model_identifier = "should-not-be-used"
api_provider = "DeepSeek"

[[models]]
name = "tx-embed"
model_identifier = "kinfra-embedding-4b"
api_provider = "TencentMaaS"

[model_task_config.planner]
model_list = ["deepseek-flash-planner", "zai-flash", "zp-4flash"]
selection_strategy = "sequential"

[model_task_config.emoji]
model_list = ["balance"]
selection_strategy = "balance"

[model_task_config.learner]
model_list = []
selection_strategy = "balance"

[model_task_config.voice]
model_list = ["balance"]
selection_strategy = "balance"

[model_task_config.replyer]
model_list = ["zai-flash", "zp-4flash", "zp-4flash-b", "or-free", "or-laguna", "ds-v4-flash", "sn-ds", "tx-hy3", "deepseek-flash-reply"]
selection_strategy = "sequential"

[model_task_config.utils]
model_list = ["zai-flash", "zp-4flash", "deepseek-flash-reply", "sn-vlm"]
selection_strategy = "sequential"

[model_task_config.fast_model]
model_list = ["zai-flash", "zp-4flash", "deepseek-flash-reply"]
selection_strategy = "sequential"

[model_task_config.memory]
model_list = ["deepseek-flash-reply"]
selection_strategy = "sequential"

[model_task_config.embedding]
model_list = ["tx-embed"]
"""


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS ", name)
    else:
        FAIL += 1
        print("  FAIL ", name, detail)


def load_patcher():
    path = ROOT / "deploy" / "apply_free_pool_routing.py"
    spec = importlib.util.spec_from_file_location("apply_free_pool_routing", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cascade_and_gate():
    print("[1] 级联顺序、质量门、ZAI 429 让位、尾部仍在")
    pairs = [(p, m) for p, _u, m, _e in botcommon.FREE_LLM_CASCADE]
    providers = [p for p, _m in pairs]
    check("智谱在 ZAI 前", providers.index("Zhipu") < providers.index("ZAI"), str(providers))
    check("默认 allow_paid 关闭", inspect.signature(botcommon.llm_free_chat).parameters["allow_paid"].default is False)
    check("免费级联没有 DeepSeek", "DeepSeek" not in providers, str(providers))
    tails = ("OpenRouter", "TencentMaaS", "SenseNova")
    check("尾部三家仍在级联", all(name in providers for name in tails), str(providers))
    check("空响应不算成功", not botcommon.cascade_result_ok("  "))
    check("纯标点不算成功", not botcommon.cascade_result_ok("……"))
    check("单字短回算成功", botcommon.cascade_result_ok("嗯") and botcommon.cascade_result_ok("好"))
    check("纯下划线不算成功", not botcommon.cascade_text_ok("___"))
    verbal = "我应该调用 reply 工具"
    check("口头说要调工具、默认不要求 tool 时正文仍算成功", botcommon.cascade_result_ok(verbal))
    check(
        "要求 tool 时没有 tool_call 不算成功",
        not botcommon.cascade_result_ok(verbal, tool_calls=[], require_tool_call=True),
    )
    check(
        "要求 tool 且有 tool_call 算成功",
        botcommon.cascade_result_ok("", tool_calls=[{"id": "1"}], require_tool_call=True),
    )

    class RateLimit(Exception):
        def __init__(self):
            super().__init__("busy")
            self.code = 429

    check("HTTP 429 认得出", botcommon.is_rate_limit_error(RateLimit()))
    check("正文里的 429 不当限流", not botcommon.is_rate_limit_error(Exception("tokens=429 today")))

    now = 1_000_000.0
    state = {"kept": 7}
    hot = botcommon.note_zai_429(state, now)
    check("429 保留其他字段", hot.get("kept") == 7, str(hot))
    planned = botcommon.plan_free_attempts(botcommon.FREE_LLM_CASCADE, hot, now + 1)
    planned_providers = [item[0] for item in planned]
    check("熔断窗口内跳过 ZAI", "ZAI" not in planned_providers, str(planned_providers))
    check("熔断后智谱仍在", "Zhipu" in planned_providers)
    check("熔断窗口内尾部仍可探（还没探过）", "OpenRouter" in planned_providers)
    cooled = botcommon.plan_free_attempts(
        botcommon.FREE_LLM_CASCADE, hot, now + botcommon.ZAI_COOLDOWN_SECONDS
    )
    check("冷却结束 ZAI 回到级联", "ZAI" in [item[0] for item in cooled])
    probed = botcommon.note_tail_probe({"kept": 7}, now)
    check("尾部探测不丢其他字段", probed.get("kept") == 7)
    quiet = botcommon.plan_free_attempts(botcommon.FREE_LLM_CASCADE, probed, now + 10)
    quiet_providers = [item[0] for item in quiet]
    check("尾部探测窗口内不再盲探", "OpenRouter" not in quiet_providers and "SenseNova" not in quiet_providers)
    check("降频时尾部名字仍在 FREE_LLM_CASCADE", all(name in providers for name in tails))
    check("降频时头层还在", quiet_providers[:2] == ["Zhipu", "Zhipu"], str(quiet_providers))
    cleared = botcommon.note_zai_success(hot)
    check("ZAI 成功清熔断并保留其他字段", cleared.get("zai_open_until") == 0 and cleared.get("kept") == 7)


def test_chat_loop_yields():
    print("[1b] llm_free_chat 真循环：空正文换层，429 之后仍能落到尾部，默认不打 DeepSeek")
    original = (
        botcommon.http_json,
        botcommon.load_provider_key,
        botcommon.tokenhub_chat_model,
        botcommon.CASCADE_CIRCUIT_STATE,
    )
    calls = []

    def fake_key(_name):
        return "test-key"

    def fake_tokenhub(default=None):
        return default

    def install(handler):
        def fake_http(url, allowed, data=None, headers=None, timeout=30):
            del url, allowed, headers, timeout
            model = json.loads(data)["model"]
            calls.append(model)
            return handler(model)
        botcommon.http_json = fake_http
        botcommon.load_provider_key = fake_key
        botcommon.tokenhub_chat_model = fake_tokenhub

    try:
        def empty_then_short(model):
            if model == "glm-4-flash-250414":
                return {"choices": [{"message": {"content": "   ", "reasoning_content": "我应该调用工具"}}]}
            if model == "glm-4-flash":
                return {"choices": [{"message": {"content": "嗯"}}]}
            raise AssertionError("不该打到 " + model)

        calls.clear()
        install(empty_then_short)
        text, provider, model = botcommon.llm_free_chat("hi", circuit_state={}, now=1_000_000)
        check("空正文（即使有 reasoning）不算成功并换层", text == "嗯" and provider == "Zhipu" and model == "glm-4-flash", str((text, provider, model, calls)))
        check("短回成功后不打 ZAI 和尾部", calls == ["glm-4-flash-250414", "glm-4-flash"], str(calls))

        def zai_429_then_tail(model):
            if model in ("glm-4-flash-250414", "glm-4-flash"):
                raise RuntimeError("zhipu down")
            if model == "glm-4.7-flash":
                err = RuntimeError("HTTP Error 429: Too Many Requests")
                err.code = 429
                raise err
            if model == "openrouter/free":
                return {"choices": [{"message": {"content": "收到"}}]}
            raise AssertionError("不该打到 " + model)

        calls.clear()
        install(zai_429_then_tail)
        text, provider, model = botcommon.llm_free_chat("hi", circuit_state={}, now=1_000_000)
        check(
            "智谱失败后 ZAI 429 仍继续，尾部接住",
            text == "收到" and provider == "OpenRouter" and model == "openrouter/free",
            str((text, provider, model, calls)),
        )
        check("这次确实打过 ZAI", "glm-4.7-flash" in calls, str(calls))
        check("默认不打付费 DeepSeek", "deepseek-flash" not in calls and "deepseek-chat" not in calls, str(calls))

        import tempfile
        state_path = Path(tempfile.mkdtemp()) / "llm_cascade_circuit_state.json"
        botcommon.CASCADE_CIRCUIT_STATE = state_path
        calls.clear()
        install(zai_429_then_tail)
        botcommon.llm_free_chat("hi", now=1_000_000)
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        check(
            "ZAI 429 落盘且尾部探测时间写上，其他键可共存",
            saved.get("zai_open_until") == 1_000_000 + botcommon.ZAI_COOLDOWN_SECONDS
            and saved.get("tail_probed_at") == 1_000_000,
            str(saved),
        )

        def tool_then_ok(model):
            if model == "glm-4-flash-250414":
                return {"choices": [{"message": {"content": "我应该调用 reply"}}]}
            if model == "glm-4-flash":
                return {"choices": [{"message": {"content": "", "tool_calls": [{"id": "call_1", "type": "function"}]}}]}
            raise AssertionError("不该打到 " + model)

        calls.clear()
        install(tool_then_ok)
        text, provider, _model = botcommon.llm_free_chat(
            "hi", require_tool_call=True, circuit_state={}, now=1_000_000,
        )
        check("要求 tool 时纯口头不算成功", "call_1" in text and provider == "Zhipu", str((text, provider, calls)))
        check("没有智谱时不挪动只有 ZAI 的列表", botcommon.move_zai_after_zhipu(
            ["zai-flash", "deepseek-flash-reply"]
        ) == ["zai-flash", "deepseek-flash-reply"])
    finally:
        (
            botcommon.http_json,
            botcommon.load_provider_key,
            botcommon.tokenhub_chat_model,
            botcommon.CASCADE_CIRCUIT_STATE,
        ) = original


def test_toml_pin(mod):
    print("[2] emoji/learner 钉免费 sequential，planner 与 voice 不动，ZAI 重试收紧")
    once = mod.apply_routing_text(FIXTURE)
    twice = mod.apply_routing_text(once)
    check("二次补丁幂等", once == twice)
    cfg = tomllib.loads(once)
    lists = cfg["model_task_config"]
    aux = lists["emoji"]["model_list"]
    expect = [
        "zp-4flash",
        "zp-4flash-b",
        "zai-flash",
        "or-free",
        "or-laguna",
        "ds-v4-flash",
        "sn-ds",
        "tx-hy3",
    ]
    check("emoji 走免费顺序池", aux == expect, str(aux))
    check("learner 与 emoji 相同", lists["learner"]["model_list"] == expect, str(lists["learner"]["model_list"]))
    check("emoji/learner 是 sequential", lists["emoji"]["selection_strategy"] == "sequential" and lists["learner"]["selection_strategy"] == "sequential")
    forbidden = {"deepseek-flash-planner", "deepseek-flash-reply", "deepseek-flash", "balance"}
    check("旁路池不含 DeepSeek 或 balance", forbidden.isdisjoint(aux), str(aux))
    check(
        "planner 整表未动且首位仍是 DeepSeek",
        lists["planner"]["model_list"] == ["deepseek-flash-planner", "zai-flash", "zp-4flash"]
        and lists["planner"]["selection_strategy"] == "sequential",
        str(lists["planner"]),
    )
    check(
        "voice 的 balance 哨兵不动（未核实为无工具文本任务）",
        lists["voice"]["model_list"] == ["balance"] and lists["voice"]["selection_strategy"] == "balance",
        str(lists["voice"]),
    )
    check("memory 仍只有付费回复模型", lists["memory"]["model_list"] == ["deepseek-flash-reply"])
    check("embedding 未动", lists["embedding"]["model_list"] == ["tx-embed"])
    check(
        "replyer 智谱在 ZAI 前且付费尾还在",
        lists["replyer"]["model_list"] == expect + ["deepseek-flash-reply"]
        and lists["replyer"]["selection_strategy"] == "sequential",
        str(lists["replyer"]["model_list"]),
    )
    check(
        "utils 让位后 vlm 仍在最后",
        lists["utils"]["model_list"] == ["zp-4flash", "zai-flash", "deepseek-flash-reply", "sn-vlm"],
        str(lists["utils"]["model_list"]),
    )
    check(
        "fast_model 让位且不丢付费尾",
        lists["fast_model"]["model_list"] == ["zp-4flash", "zai-flash", "deepseek-flash-reply"],
        str(lists["fast_model"]["model_list"]),
    )
    providers = {item["name"]: item for item in cfg["api_providers"]}
    check("ZAI max_retry 收到 1", providers["ZAI"]["max_retry"] == 1, str(providers["ZAI"].get("max_retry")))
    check("ZAI retry_interval 收到 1", providers["ZAI"]["retry_interval"] == 1, str(providers["ZAI"].get("retry_interval")))
    check("智谱重试次数不动", providers["Zhipu"]["max_retry"] == 2 and providers["Zhipu"]["retry_interval"] == 5)
    check("密钥原样", providers["ZAI"]["api_key"] == "not-a-real-key" and providers["Zhipu"]["api_key"] == "also-not-real")
    try:
        mod.apply_routing_text('name = "no-models"\n')
        check("没有免费模型时拒绝写空列表", False, "竟然成功了")
    except RuntimeError as e:
        check("没有免费模型时拒绝写空列表", "空列表" in str(e), str(e))


if __name__ == "__main__":
    patcher = load_patcher()
    test_cascade_and_gate()
    test_chat_loop_yields()
    test_toml_pin(patcher)
    print("\n结果: %d passed, %d failed" % (PASS, FAIL))
    raise SystemExit(1 if FAIL else 0)
