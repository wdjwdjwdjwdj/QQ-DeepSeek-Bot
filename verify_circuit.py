# -*- coding: utf-8 -*-
"""verify_circuit.py — 模型熔断补丁实测

在隔离的临时实例目录里（只复制 config），驱动真实 LLM 请求三轮，
观察：死模型快速失败 → 连续 2 次触发熔断 → 后续轮次直接跳过死模型。
不影响正在运行的 MaiBot 实例。

用法：用 MaiBot 的 venv python 运行：
    "C:\\Users\\29688\\AppData\\Roaming\\uv\\tools\\maibot\\Scripts\\python.exe" verify_circuit.py
"""
import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_SRC = BASE / "my-maibot" / "config"
VENV_PY = Path(r"C:\Users\29688\AppData\Roaming\uv\tools\maibot\Scripts\python.exe")


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="maibot_circuit_"))
    shutil.copytree(CONFIG_SRC, tmp / "config")
    os.environ["MAIBOT_INSTANCE_DIR"] = str(tmp)

    from src.llm_models.utils_model import LLMOrchestrator, _model_circuit, _circuit_is_blocked

    prompt = "只回复两个字：收到"
    print("=== 三轮真实请求（utils 池，含已死的商汤模型）===\n")
    for round_no in range(1, 4):
        t0 = time.time()
        orch = LLMOrchestrator("utils")
        result = await orch.generate_response_async(prompt)
        cost = time.time() - t0
        blocked = {m for m in _model_circuit if _circuit_is_blocked(m)}
        print(
            f"第{round_no}轮: 用时 {cost:5.1f}s | 实际模型 {result.model_name:<12} | "
            f"熔断中: {sorted(blocked) if blocked else '无'}"
        )

    print("\n熔断表:", {k: (v[0], time.strftime('%H:%M:%S', time.localtime(v[1]))) for k, v in _model_circuit.items()})
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
