# -*- coding: utf-8 -*-
"""MaiBot 进程启动时挂上 person_memory 薄包装。

由 deploy/install_person_memory_hook.py 写进 MaiBot 虚拟环境的 .pth，
让解释器启动时 import 本模块。FriendLedger.on_load 也会再调一次 install()，
重复调用无害。

网络边界：不出网。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import person_memory  # noqa: E402

person_memory.install()
