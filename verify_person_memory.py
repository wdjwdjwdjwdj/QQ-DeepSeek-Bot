# -*- coding: utf-8 -*-
"""校验按人记忆隔离：fail-closed、认人、关系账本、只读审计。

用法：
  python3 verify_person_memory.py

网络边界：不出网、不发 QQ、不写记忆库。
"""
from __future__ import annotations

import asyncio
import copy
import sqlite3
import subprocess
import sys
from pathlib import Path

import person_audit
import person_memory as pm


def test_filter_fail_closed():
    hits = [
        {"content": "别人的事", "metadata": {"person_ids": ["bbb"], "chat_id": "c1"}},
        {"content": "本群天气", "metadata": {"person_ids": [], "chat_id": "c1"}},
        {"content": "别的会话", "metadata": {"chat_id": "z9"}},
        {"content": "正文里碰巧有 aaa", "metadata": {"person_ids": ["bbb"], "chat_id": "c1"}},
    ]
    # 滤空不得退回全部 hits（上游 filtered or hits 会把这四条都留下）
    assert pm.filter_person_hits(hits, "aaa", chat_id="") == []
    only_chat = pm.filter_person_hits(hits, "aaa", chat_id="c1")
    assert only_chat == [hits[1]], only_chat
    # 已标成 bbb 的两条都算 bbb；正文里的 aaa 不能把别人的标注抢走
    assert pm.filter_person_hits(hits, "bbb", chat_id="c1") == [hits[0], hits[3]]
    # 未标注人物、正文含 id，算命中
    loose = [{"content": "记得 aaa 说过", "metadata": {"chat_id": "c1"}}]
    assert pm.filter_person_hits(loose, "aaa", chat_id="c1") == loose
    # 没有 person 意图时原样返回
    assert pm.filter_person_hits(hits, "", chat_id="c1") == hits
    print("OK filter fail-closed")


def test_block_unscoped_retry():
    assert pm.should_block_unscoped_retry("旅行", "旅行", "") is True
    assert pm.should_block_unscoped_retry("旅行", "别的", "") is False
    assert pm.should_block_unscoped_retry("旅行", "旅行", "pid") is False
    assert pm.should_block_unscoped_retry(None, "旅行", "") is False

    class Result:
        def __init__(self, hits, success=True, filtered=False):
            self.hits = hits
            self.success = success
            self.filtered = filtered
            self.summary = "sum"
            self.error = ""

    calls = []

    async def orig(query, **kwargs):
        calls.append((query, kwargs.get("person_id")))
        if kwargs.get("person_id"):
            return Result([])
        return Result([{"content": "全库", "metadata": {"person_ids": ["other"]}}])

    wrapped = pm.guard_search(orig, lambda: Result([]))

    async def run():
        pm._PENDING_UNSCOPED.set(None)
        first = await wrapped("旅行", person_id="pid", chat_id="c1")
        assert first.hits == []
        second = await wrapped("旅行", person_id="", chat_id="c1")
        assert second.hits == []
        assert calls == [("旅行", "pid")]
        # 不同 query 的无人物检索仍然放行
        third = await wrapped("天气", person_id="", chat_id="c1")
        assert third.hits and third.hits[0]["content"] == "全库"

    asyncio.run(run())
    print("OK query_memory 未命中不清 person_id")


def test_resolve_user_id_over_name():
    records = [
        {"person_id": "first", "person_name": "小明"},
        {"person_id": "second", "person_name": "小明"},
        {"person_id": "only", "person_name": "阿吉"},
    ]
    uid = pm.person_id_from_platform_user("qq", "2968874474")
    # 就算名字能对上别人，有 platform+user_id 也不许被昵称盖掉
    got = pm.resolve_person_id_for_memory(
        person_name="小明", platform="qq", user_id="2968874474", records=records,
    )
    assert got == uid, got
    assert got != "first"
    # 按名命中 2 个：不取 limit(1) 的第一条
    assert records[0]["person_id"] == "first"
    assert pm.resolve_person_id_for_memory(person_name="小明", records=records) == ""
    assert pm.resolve_person_id_for_memory(person_name="阿吉", records=records) == "only"
    assert pm.resolve_person_id_for_memory(person_name="没有", records=records) == ""
    assert pm.ids_by_person_name(records, "小明") == ["first", "second"]
    print("OK resolve user_id 优先 / 重名歧义")


def test_relation_no_flatten():
    ledger = {
        "groups": {
            "111111": {
                "members": {
                    "10001": {"name": "小明", "score": 1, "tier": "普通", "last_change": "g1"},
                }
            },
            "222222": {
                "members": {
                    "20002": {"name": "小明", "score": 9, "tier": "重点朋友", "last_change": "g2"},
                    "10001": {"name": "小明", "score": 4, "tier": "聊得来", "last_change": "g2b"},
                }
            },
        }
    }
    amb = pm.relation_lookup(ledger, "小明")
    assert amb["status"] == "ambiguous", amb
    assert {row["qq"] for row in amb["matches"]} == {"10001", "20002"}
    assert {row["group_id"] for row in amb["matches"]} == {"111111", "222222"}

    scoped = pm.relation_lookup(ledger, "10001", group_id="222222")
    assert scoped["status"] == "one", scoped
    assert scoped["matches"][0]["score"] == 4
    assert scoped["matches"][0]["group_id"] == "222222"

    # 当前群没有这个 QQ：列出别的群，不暗绑过去
    other = pm.relation_lookup(ledger, "10001", group_id="333333")
    assert other["status"] == "ambiguous", other
    assert len(other["matches"]) == 2

    self_row = pm.relation_lookup(ledger, "", user_id="10001", group_id="111111")
    assert self_row["status"] == "self"
    assert self_row["matches"][0]["score"] == 1
    print("OK 账本 QQ 优先 / 不跨群暗绑")


def test_audit_readonly(tmp=Path("/tmp/person_audit_verify")):
    tmp.mkdir(exist_ok=True)
    db = tmp / "MaiBot.db"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE person_info (person_id TEXT, platform TEXT, user_id TEXT, "
        "person_name TEXT, user_nickname TEXT, group_cardname TEXT)"
    )
    con.execute(
        "INSERT INTO person_info VALUES (?,?,?,?,?,?)",
        ("p1", "qq", "10001", "小明", "明", ""),
    )
    con.execute(
        "INSERT INTO person_info VALUES (?,?,?,?,?,?)",
        ("p2", "qq", "10002", "小明", "明2", ""),
    )
    con.commit()
    con.close()
    before = db.read_bytes()
    persons = person_audit.load_persons(db)
    ledger = {
        "groups": {
            "111111": {
                "members": {
                    "10001": {"name": "小明", "score": 1, "tier": "普通"},
                    "99999": {"name": "路人", "score": 0, "tier": "普通"},
                }
            }
        }
    }
    ledger_copy = copy.deepcopy(ledger)
    persons_copy = copy.deepcopy(persons)
    report = pm.audit_person_ledger(persons, ledger)
    assert persons == persons_copy
    assert ledger == ledger_copy
    assert report["deleted"] == 0
    assert "99999" in report["friends_missing_in_person_info"]
    assert any(item["person_name"] == "小明" and len(item["person_ids"]) == 2 for item in report["ambiguous_person_names"])
    assert db.read_bytes() == before
    skipped = person_audit.load_persons(tmp / "missing.db")
    assert skipped is None
    print("OK 审计只读")


def test_scrub_fallback_banner():
    class Result:
        def __init__(self):
            self.content = (
                "提示：人物定向检索未命中，已自动降级为关键词检索。\n"
                "1. 别人\n2. 本群天气"
            )
            self.structured_content = {
                "fallback_applied": True,
                "fallback_reason": "person_filter_miss",
                "person_id": "aaa",
                "chat_id": "c1",
                "summary": "别人的摘要",
                "hits": [
                    {"content": "别人", "metadata": {"person_ids": ["bbb"], "chat_id": "c1"}},
                    {"content": "本群天气", "metadata": {"person_ids": [], "chat_id": "c1"}},
                ],
            }
            self.metadata = {"replyer_memory_reference": "不该带给回复"}

    out = pm.scrub_false_person_fallback(Result())
    assert "降级" not in out.content and "别人" not in out.content
    assert "本群天气" in out.content
    assert out.structured_content["fallback_applied"] is False
    assert out.structured_content["hits"] == [
        {"content": "本群天气", "metadata": {"person_ids": [], "chat_id": "c1"}}
    ]
    assert "replyer_memory_reference" not in out.metadata
    print("OK 降级提示被拿掉")


def test_install_import_hook():
    script = "import person_memory; person_memory.install(); import json, pathlib; print('hook-ok')"
    out = subprocess.check_output([sys.executable, "-c", script], text=True)
    assert "hook-ok" in out
    print("OK install 不挡普通 import")


def main():
    test_filter_fail_closed()
    test_block_unscoped_retry()
    test_resolve_user_id_over_name()
    test_relation_no_flatten()
    test_audit_readonly()
    test_scrub_fallback_banner()
    test_install_import_hook()
    # 脚本本身不得出现删库语句
    src = Path(__file__).resolve().parent.joinpath("person_audit.py").read_text(encoding="utf-8")
    assert "DELETE FROM" not in src
    assert "unlink" not in src
    print("ALL verify_person_memory OK")


if __name__ == "__main__":
    main()
