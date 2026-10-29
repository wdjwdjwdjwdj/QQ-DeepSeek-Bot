# -*- coding: utf-8 -*-
"""校验 planner 池首位是 tool-capable DeepSeek 规划模型。

调用：手工/冒烟；非 cron。
用法：python3 verify_planner_pool.py
网络边界：只读本地 model_config.toml，不出网。
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

CFG = Path(__file__).resolve().parent / "my-maibot/config/model_config.toml"
EXPECT_HEAD = "deepseek-flash-planner"


def main() -> int:
    if not CFG.is_file():
        print(f"MISSING {CFG}")
        return 2
    data = tomllib.loads(CFG.read_text(encoding="utf-8"))
    models = data["model_task_config"]["planner"]["model_list"]
    print("planner.model_list =", models)
    if not models or models[0] != EXPECT_HEAD:
        print(f"FAIL: planner[0] should be {EXPECT_HEAD!r}, got {models[:1]!r}")
        return 1
    print("OK: planner head is tool-capable DeepSeek")
    return 0


if __name__ == "__main__":
    sys.exit(main())
