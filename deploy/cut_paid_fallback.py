# -*- coding: utf-8 -*-
"""cut_paid_fallback.py — 把付费 deepseek-flash 从 planner/utils 池移除（一次性）

背景：免费池（商汤额度耗尽/智谱腾讯TPM限流）一死，balance 策略就轮到付费
官方模型兜底，planner 单次 4-6K token，一天烧 1.95 元。移除后这两个任务
免费池专属：全死时任务重试等待（分钟级 TPM 恢复），绝不烧钱。
replyer 保留 deepseek-flash（文风定稿）+ 商汤免费备用，不受影响。
流程：备份 → 锚点替换 → tomllib 校验 → os.replace 原子落盘。
已于 2026-10-25 21:49 在服务器执行并验证（热重载生效，付费调用清零）。
"""
import shutil
import tomllib
from pathlib import Path

CFG = Path("/root/maibot/my-maibot/config/model_config.toml")
BAK = CFG.with_name("model_config.toml.bak-免费池专属调参")

text = CFG.read_text(encoding="utf-8")

pairs = [
    # planner
    ('model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "zp-flash", "zp-flashx", "tx-hy3", "deepseek-flash"] # 使用的模型列表, 每个元素对应上面的模型名称(name) — 双模型分流抗限频',
     'model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "zp-flash", "zp-flashx", "tx-hy3"] # 使用的模型列表, 每个元素对应上面的模型名称(name) — 免费池专属：付费模型已移除，防兜底烧钱（2026-10-25）'),
    # utils
    ('model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "zp-flash", "zp-flashx", "tx-hy3", "deepseek-flash"] # 使用的模型列表 — 三模型分流抗限频，小任务不与主对话抢配额',
     'model_list = ["glm-52", "kimi-k3", "ds-v4-flash", "zp-flash", "zp-flashx", "tx-hy3"] # 使用的模型列表 — 免费池专属：付费模型已移除，防兜底烧钱（2026-10-25）'),
]

applied = 0
for old, new in pairs:
    if old in text:
        text = text.replace(old, new, 1)
        applied += 1
    else:
        print("ANCHOR_MISS:", old[:60])

assert applied == 2, f"锚点命中 {applied}/2，中止"

shutil.copy(CFG, BAK)
tmp = CFG.with_suffix(".toml.tmp")
tmp.write_text(text, encoding="utf-8")
with open(tmp, "rb") as f:
    cfg = tomllib.load(f)
tmp.replace(CFG)

# 回读验证：planner/utils 的池里不应再有 deepseek-flash
mt = cfg["model_task_config"]
for task in ("planner", "utils"):
    lst = mt[task]["model_list"]
    assert "deepseek-flash" not in lst, task + " 仍含付费模型"
    print(task, "=", lst)
print("replyer =", mt["replyer"]["model_list"], "（保持付费文风）")
print(f"APPLIED=2/2, backup={BAK.name}")
