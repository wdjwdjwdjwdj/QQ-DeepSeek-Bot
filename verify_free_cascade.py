# -*- coding: utf-8 -*-
"""校验 TokenHub tx-hy3 进免费级联，且对话池补丁不碰 planner / embedding。

用法：python3 verify_free_cascade.py
网络边界：不出网、不发 QQ、不读生产密钥。
"""
import importlib.util
import tomllib
from pathlib import Path

import botcommon

ROOT = Path(__file__).resolve().parent
PASS = 0
FAIL = 0

FIXTURE = """
[[api_providers]]
name = "TencentMaaS"
base_url = "https://tokenhub.tencentmaas.com/v1"
api_key = "not-a-real-key"
client_type = "openai"

[[models]]
model_type = "llm"
model_identifier = "hy3"
name = "tx-hy3"
api_provider = "TencentMaaS"
price_in = 0
price_out = 0

[[models]]
model_type = "embedding"
model_identifier = "kinfra-embedding-4b"
name = "tx-embed"
api_provider = "TencentMaaS"
price_in = 0
price_out = 0

[model_task_config.planner]
model_list = ["deepseek-flash-planner", "zai-flash"]

[model_task_config.replyer]
model_list = ["zai-flash", "zp-4flash", "deepseek-flash-reply"]

[model_task_config.utils]
model_list = ["zai-flash", "zp-4flash", "deepseek-flash-reply", "sn-vlm"]

[model_task_config.fast_model]
model_list = ["zai-flash", "deepseek-flash-reply"]

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
    path = ROOT / "deploy" / "add_tokenhub_chat.py"
    spec = importlib.util.spec_from_file_location("add_tokenhub_chat", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cascade_order():
    print("[1] FREE_LLM_CASCADE：TokenHub 在 OpenRouter 之后、商汤之前")
    pairs = [(p, m) for p, _u, m, _e in botcommon.FREE_LLM_CASCADE]
    check("级联含 TencentMaaS/hy3", ("TencentMaaS", "hy3") in pairs, str(pairs))
    i_or = max(i for i, item in enumerate(pairs) if item[0] == "OpenRouter")
    i_tx = pairs.index(("TencentMaaS", "hy3"))
    i_sn = pairs.index(("SenseNova", "deepseek-v4-flash"))
    check("OpenRouter < TokenHub < SenseNova", i_or < i_tx < i_sn, str((i_or, i_tx, i_sn)))
    check("首位仍是 ZAI", pairs[0][0] == "ZAI", pairs[0][0])
    check("planner 付费级联未插入 TokenHub", all(p != "TencentMaaS" for p, _m in [
        (a, c) for a, _b, c, _d in botcommon.PAID_LLM_CASCADE
    ]))
    host = "tokenhub.tencentmaas.com"
    check("白名单含 tokenhub", host in botcommon.ALLOWED_LLM_FREE, str(botcommon.ALLOWED_LLM_FREE))
    check("缺省 identifier 为 hy3", botcommon.tokenhub_chat_model() == "hy3")


def test_pool_patch(mod):
    print("[2] 对话池中后段插入 tx-hy3，planner / embedding 不动")
    check(
        "插在付费尾之前",
        mod.insert_tokenhub_chat(["zai-flash", "zp-4flash", "deepseek-flash-reply"])
        == ["zai-flash", "zp-4flash", "tx-hy3", "deepseek-flash-reply"],
    )
    check(
        "vlm 仍在最后",
        mod.insert_tokenhub_chat(["zai-flash", "deepseek-flash-reply", "sn-vlm"])
        == ["zai-flash", "tx-hy3", "deepseek-flash-reply", "sn-vlm"],
    )
    once = mod.patch_model_config_text(FIXTURE)
    twice = mod.patch_model_config_text(once)
    check("二次补丁幂等", once == twice)
    cfg = tomllib.loads(once)
    lists = cfg["model_task_config"]
    check(
        "replyer 中后段",
        lists["replyer"]["model_list"] == ["zai-flash", "zp-4flash", "tx-hy3", "deepseek-flash-reply"],
        str(lists["replyer"]["model_list"]),
    )
    check(
        "utils 在 vlm 前",
        lists["utils"]["model_list"]
        == ["zai-flash", "zp-4flash", "tx-hy3", "deepseek-flash-reply", "sn-vlm"],
        str(lists["utils"]["model_list"]),
    )
    check(
        "fast_model 中后段",
        lists["fast_model"]["model_list"] == ["zai-flash", "tx-hy3", "deepseek-flash-reply"],
        str(lists["fast_model"]["model_list"]),
    )
    check(
        "planner 整表未动",
        lists["planner"]["model_list"] == ["deepseek-flash-planner", "zai-flash"],
        str(lists["planner"]["model_list"]),
    )
    check("planner 首位仍是 DeepSeek", lists["planner"]["model_list"][0] == "deepseek-flash-planner")
    check("embedding 未动", lists["embedding"]["model_list"] == ["tx-embed"])
    idents = {m["name"]: m["model_identifier"] for m in cfg["models"]}
    check("embedding 模型标识未改", idents["tx-embed"] == "kinfra-embedding-4b")
    check("未新增 provider", len(cfg["api_providers"]) == 1)


def test_toml_identifier(mod):
    print("[3] toml 里的 tx-hy3 identifier 覆盖级联默认值")
    text = FIXTURE.replace('model_identifier = "hy3"', 'model_identifier = "hy3-preview"', 1)
    cfg_path = botcommon.MODEL_CONFIG
    # 只在临时文件上读，不碰真实路径的写入；函数认的是 MODEL_CONFIG，测完还原。
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "model_config.toml"
        path.write_text(text, encoding="utf-8")
        old = botcommon.MODEL_CONFIG
        botcommon.MODEL_CONFIG = path
        try:
            got = botcommon.tokenhub_chat_model()
        finally:
            botcommon.MODEL_CONFIG = old
    check("改读 toml identifier", got == "hy3-preview", got)
    try:
        mod.patch_model_config_text(FIXTURE.replace('name = "tx-hy3"', 'name = "tx-other"', 1))
        check("缺少 tx-hy3 时拒绝", False, "竟然改写了")
    except RuntimeError as e:
        check("缺少 tx-hy3 时拒绝", "tx-hy3" in str(e), str(e))


if __name__ == "__main__":
    patcher = load_patcher()
    test_cascade_order()
    test_pool_patch(patcher)
    test_toml_identifier(patcher)
    print("\n结果: %d passed, %d failed" % (PASS, FAIL))
    raise SystemExit(1 if FAIL else 0)
