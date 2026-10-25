# -*- coding: utf-8 -*-
"""add_sensenova_models.py — 商汤全部可用模型接入（一次性）

商汤免费目录 7 模型，原用 3 个（glm-5.2/kimi-k3/deepseek-v4-flash + vlm 岗 6.8-lite）。
新增：deepseek-v4.1-flash(sn-ds41)、deepseek-flash(sn-ds，商汤版，避免与付费官方
重名)、sensenova-u1.5-lite(sn-u15)；6.8-flash-lite 以现有别名 sn-vlm 兼职文本。
接入：planner/utils 池扩充（sequential 免费优先、付费官方仍殿尾），replyer 增加
商汤 deepseek 系备用尾（官方 429 时文风最接近的兜底）。
注：商汤额度为账号级（1113 一起死），扩容收益=健康期分摊单模型限流+池子纵深；
新增条目若不在套餐内，熔断补丁会自动跳过，无副作用。明日额度重置后自证。
流程：备份 → 锚点替换 → tomllib 校验 → os.replace 原子落盘。
"""
import shutil
import tomllib
from pathlib import Path

CFG = Path("/root/maibot/my-maibot/config/model_config.toml")
BAK = CFG.with_name("model_config.toml.bak-商汤全家桶")

text = CFG.read_text(encoding="utf-8")
applied = 0

# 1) planner 池扩充
old_p = 'model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "zp-flash", "zp-flashx", "tx-hy3", "deepseek-flash"] # 使用的模型列表, 每个元素对应上面的模型名称(name) — sequential免费优先，deepseek-flash仅池尾兜底（2026-10-25）'
new_p = 'model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "sn-ds41", "sn-ds", "sn-u15", "zp-flash", "zp-flashx", "tx-hy3", "deepseek-flash"] # 使用的模型列表, 每个元素对应上面的模型名称(name) — sequential免费优先（商汤全家桶+智谱+腾讯），deepseek-flash仅池尾兜底（2026-10-25）'
if old_p in text:
    text = text.replace(old_p, new_p, 1); applied += 1
else:
    print("ANCHOR_MISS planner")

# 2) utils 池扩充（含 sn-vlm 文本兼职）
old_u = 'model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "zp-flash", "zp-flashx", "tx-hy3", "deepseek-flash"] # 使用的模型列表 — sequential免费优先，deepseek-flash仅池尾兜底（2026-10-25）'
new_u = 'model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "sn-ds41", "sn-ds", "sn-u15", "sn-vlm", "zp-flash", "zp-flashx", "tx-hy3", "deepseek-flash"] # 使用的模型列表 — sequential免费优先（商汤全家桶+智谱+腾讯），deepseek-flash仅池尾兜底（2026-10-25）'
if old_u in text:
    text = text.replace(old_u, new_u, 1); applied += 1
else:
    print("ANCHOR_MISS utils")

# 3) replyer 备用尾扩充
old_r = 'model_list = ["deepseek-flash", "ds-v4-flash"] # 使用的模型列表（池尾=商汤备用，仅官方故障时启用）, 每个元素对应上面的模型名称(name)'
new_r = 'model_list = ["deepseek-flash", "ds-v4-flash", "sn-ds41", "sn-ds"] # 使用的模型列表（官方优先；池尾=商汤 deepseek 系备用，仅官方故障时启用）, 每个元素对应上面的模型名称(name)'
if old_r in text:
    text = text.replace(old_r, new_r, 1); applied += 1
else:
    print("ANCHOR_MISS replyer")

# 4) 追加 3 个新 [[models]] 条目到文件尾
NEW_BLOCKS = '''

# ==== 商汤全家桶扩充（2026-10-25，免费目录 7 模型全接入）====

[[models]]
model_type = "llm"
model_identifier = "deepseek-v4.1-flash"
name = "sn-ds41"
api_provider = "SenseNova"
price_in = 0
cache = false
cache_price_in = 0
price_out = 0
price_periods = []
send_temperature = true
force_stream_mode = false
visual = false

[models.extra_params]

[[models]]
model_type = "llm"
model_identifier = "deepseek-flash"
name = "sn-ds"
api_provider = "SenseNova"
price_in = 0
cache = false
cache_price_in = 0
price_out = 0
price_periods = []
send_temperature = true
force_stream_mode = false
visual = false

[models.extra_params]

[[models]]
model_type = "llm"
model_identifier = "sensenova-u1.5-lite"
name = "sn-u15"
api_provider = "SenseNova"
price_in = 0
cache = false
cache_price_in = 0
price_out = 0
price_periods = []
send_temperature = true
force_stream_mode = false
visual = false

[models.extra_params]
'''
if 'model_identifier = "deepseek-v4.1-flash"' not in text:
    text = text.rstrip() + "\n" + NEW_BLOCKS
    applied += 1
else:
    print("新条目已存在，跳过追加")

assert applied == 4, f"锚点命中 {applied}/4，中止"

shutil.copy(CFG, BAK)
tmp = CFG.with_suffix(".toml.tmp")
tmp.write_text(text, encoding="utf-8")
with open(tmp, "rb") as f:
    cfg = tomllib.load(f)
tmp.replace(CFG)

names = [m["name"] for m in cfg["models"]]
for alias in ("sn-ds41", "sn-ds", "sn-u15"):
    assert alias in names, alias + " 条目缺失"
mt = cfg["model_task_config"]
print("models 总数:", len(names))
print("planner =", mt["planner"]["model_list"])
print("utils   =", mt["utils"]["model_list"])
print("replyer =", mt["replyer"]["model_list"])
print(f"APPLIED={applied}/4, backup={BAK.name}")
