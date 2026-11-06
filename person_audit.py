# -*- coding: utf-8 -*-
"""person_audit.py — person_info 与 friends.json 只读对照

功能：把 MaiBot 的 person_info 和关系账本 friends.json 并排看一遍，列出
账本有号但人物表没有、名字对不上、同名对应多个 person_id / 多个 QQ。
只打印报告，不删记忆、不改两份数据。

调用：不挂计划任务。需要时手工跑::

    python3 person_audit.py
    python3 person_audit.py --db /path/MaiBot.db --ledger /path/friends.json

网络边界：不出网、不发 QQ、不写数据库。
"""
import json
import sqlite3
import sys
from pathlib import Path

import person_memory

BASE = Path(__file__).resolve().parent
DEFAULT_DB = BASE / "my-maibot" / "data" / "MaiBot.db"
DEFAULT_LEDGER = BASE / "friends.json"


def _arg(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1])
    return default


def load_persons(db_path: Path):
    """只读打开。库或表不在就返回 None，不创建文件。"""
    if not db_path.is_file():
        return None
    uri = db_path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "person_info" not in tables:
            return None
        rows = con.execute(
            "SELECT person_id, platform, user_id, person_name, user_nickname, group_cardname "
            "FROM person_info"
        ).fetchall()
    finally:
        con.close()
    out = []
    for person_id, platform, user_id, person_name, user_nickname, group_cardname in rows:
        out.append(
            {
                "person_id": person_id or "",
                "platform": platform or "",
                "user_id": str(user_id or ""),
                "person_name": person_name or "",
                "user_nickname": user_nickname or "",
                "group_cardname": group_cardname or "",
            }
        )
    return out


def load_ledger(path: Path):
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    db_path = _arg("--db", DEFAULT_DB)
    ledger_path = _arg("--ledger", DEFAULT_LEDGER)
    persons = load_persons(db_path)
    ledger = load_ledger(ledger_path)
    if persons is None or ledger is None:
        report = {
            "status": "skipped",
            "db": str(db_path),
            "ledger": str(ledger_path),
            "db_ready": persons is not None,
            "ledger_ready": ledger is not None,
            "deleted": 0,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    report = person_memory.audit_person_ledger(persons, ledger)
    report["status"] = "ok"
    report["db"] = str(db_path)
    report["ledger"] = str(ledger_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
