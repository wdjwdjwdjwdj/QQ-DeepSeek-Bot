# -*- coding: utf-8 -*-
"""sequential_strategy.py — 三任务池改 sequential 顺序优先（一次性）

用户关切：①不希望回复质量下降 ②回复次数多。
修正：planner/utils 由 balance（均匀轮载，付费模型占 1/7 流量）改 sequential
（免费优先、付费殿后，仅全免费失败时兜底）；replyer 由 random（付费官方与
商汤备用各半，文风漂移）改 sequential（100% DeepSeek 官方优先，官方故障才
落商汤备用）。
流程：备份 → 锚点替换 → tomllib 校验 → os.replace 原子落盘。
"""
import shutil
import tomllib
from pathlib import Path

CFG = Path("/root/maibot/my-maibot/config/model_config.toml")
BAK = CFG.with_name("model_config.toml.bak-sequential调参")

text = CFG.read_text(encoding="utf-8")

applied = 0

# 1) planner/utils 的 model_list：deepseek-flash 回到池尾（sequential 下仅兜底）
pairs = [
    ('model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "zp-flash", "zp-flashx", "tx-hy3"] # 使用的模型列表, 每个元素对应上面的模型名称(name) — 免费池专属：付费模型已移除，防兜底烧钱（2026-10-25）',
     'model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "zp-flash", "zp-flashx", "tx-hy3", "deepseek-flash"] # 使用的模型列表, 每个元素对应上面的模型名称(name) — sequential免费优先，deepseek-flash仅池尾兜底（2026-10-25）'),
    ('model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "zp-flash", "zp-flashx", "tx-hy3"] # 使用的模型列表 — 免费池专属：付费模型已移除，防兜底烧钱（2026-10-25）',
     'model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "zp-flash", "zp-flashx", "tx-hy3", "deepseek-flash"] # 使用的模型列表 — sequential免费优先，deepseek-flash仅池尾兜底（2026-10-25）'),
]
for old, new in pairs:
    if old in text:
        text = text.replace(old, new, 1)
        applied += 1
    else:
        print("ANCHOR_MISS:", old[:60])

# 2) planner/utils 块内 selection_strategy：balance → sequential
blocks = text.split("[model_task_config.")
for i, b in enumerate(blocks):
    if b.startswith("planner]") or b.startswith("utils]"):
        b2 = b.replace('selection_strategy = "balance"', 'selection_strategy = "sequential"', 1)
        if b2 != b:
            applied += 1
        blocks[i] = b2
text = "[model_task_config.".join(blocks)

# 3) replyer 块内 selection_strategy：random → sequential（文风稳定）
blocks = text.split("[model_task_config.")
for i, b in enumerate(blocks):
    if b.startswith("replyer]"):
        b2 = b.replace('selection_strategy = "random"', 'selection_strategy = "sequential"', 1)
        if b2 != b:
            applied += 1
        blocks[i] = b2
text = "[model_task_config.".join(blocks)

assert applied == 5, f"锚点命中 {applied}/5，中止"

shutil.copy(CFG, BAK)
tmp = CFG.with_suffix(".toml.tmp")
tmp.write_text(text, encoding="utf-8")
with open(tmp, "rb") as f:
    cfg = tomllib.load(f)
tmp.replace(CFG)

mt = cfg["model_task_config"]
for task in ("planner", "utils", "replyer"):
    print(task, "strategy =", mt[task]["selection_strategy"], "| list =", mt[task]["model_list"])
print(f"APPLIED={applied}/5, backup={BAK.name}")
