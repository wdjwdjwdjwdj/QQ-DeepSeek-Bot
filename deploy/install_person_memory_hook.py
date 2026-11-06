# -*- coding: utf-8 -*-
"""把人物记忆薄包装挂进 MaiBot 解释器的 site-packages。

功能：在 uv 工具环境的 site-packages 写一条 .pth，启动时 import
deploy/hooks/person_memory_hook.py。幂等。找不到 MaiBot 环境就 SKIP，
FriendLedger.on_load 仍会在机器人进程里调用 person_memory.install()。

调用：服务器上手工执行（非 cron）::

    python3 deploy/install_person_memory_hook.py

网络边界：不出网、不发 QQ、不改记忆库。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_DIR = ROOT / "deploy" / "hooks"
PTH_NAME = "zz_person_memory_hook.pth"
PTH_BODY = str(HOOK_DIR) + "\nimport person_memory_hook\n"


def _site_packages_under(root: Path):
    if not root.is_dir():
        return []
    return sorted(root.glob("lib/python*/site-packages"))


def candidate_site_packages():
    roots = [
        Path("/root/.local/share/uv/tools/maibot"),
        Path.home() / ".local/share/uv/tools/maibot",
    ]
    found = []
    for root in roots:
        found.extend(_site_packages_under(root))
    return found


def install_into(site_packages: Path):
    path = site_packages / PTH_NAME
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current == PTH_BODY:
        print("OK 已挂好", path)
        return
    path.write_text(PTH_BODY, encoding="utf-8")
    print("OK 写入", path)


def main():
    if not (HOOK_DIR / "person_memory_hook.py").is_file():
        print("缺少 deploy/hooks/person_memory_hook.py", file=sys.stderr)
        return 1
    sites = candidate_site_packages()
    if not sites:
        print("SKIP 没有找到 MaiBot 的 site-packages（FriendLedger.on_load 仍会 install）")
        return 0
    for site in sites:
        install_into(site)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
