# -*- coding: utf-8 -*-
"""person_memory.py — 按人记忆隔离（薄包装，不 fork MaiBot / A_Memorix）

功能：给上游检索补上 fail-closed。带 person 意图时，过滤失败只留空或本 chat
的非人物事实，禁止「filtered or hits」把别人的命中放行；query_memory 在
人物检索未命中时，禁止清空 person_id 再搜全库。认人时已有 platform+user_id
不得被昵称覆盖；按名命中多于 1 个则歧义，不 limit(1) 暗选。

调用：不是计划任务。MaiBot 进程里由 FriendLedger.on_load 与
deploy/hooks/person_memory_hook.py 调用 install()。单测走 verify_person_memory.py。
person_audit.py 复用本模块的只读对照。

网络边界：本模块不出网、不发 QQ、不写记忆库、不删记忆。
"""
from __future__ import annotations

import contextvars
import hashlib
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

# 人物定向检索进行中：同一查询紧接着的「清空 person_id」重试要拦住。
_PENDING_UNSCOPED = contextvars.ContextVar("person_memory_pending_unscoped", default=None)
_SEARCH_CHAT = contextvars.ContextVar("person_memory_search_chat", default="")

_INSTALLED = False


def person_id_from_platform_user(platform: str, user_id: Any) -> str:
    """与 MaiBot get_person_id 相同：平台名去掉首段适配器前缀后，md5(platform_user)。"""
    plat = str(platform or "").strip()
    uid = str(user_id or "").strip()
    if "-" in plat:
        plat = plat.split("-")[1]
    return hashlib.md5(f"{plat}_{uid}".encode()).hexdigest()


def person_ids_of(hit: Mapping[str, Any]) -> List[str]:
    meta = hit.get("metadata") or {}
    if not isinstance(meta, Mapping):
        meta = {}
    raw = meta.get("person_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    out: List[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def chat_ids_of(hit: Mapping[str, Any]) -> List[str]:
    meta = hit.get("metadata") or {}
    if not isinstance(meta, Mapping):
        meta = {}
    out: List[str] = []
    for key in ("chat_id", "stream_id", "session_id"):
        for src in (hit, meta):
            text = str(src.get(key) or "").strip() if isinstance(src, Mapping) else ""
            if text and text not in out:
                out.append(text)
    return out


def hit_matches_person(hit: Mapping[str, Any], person_id: str) -> bool:
    """已标注人物时只认 person_ids；未标注才允许正文里出现该 id。"""
    pid = str(person_id or "").strip()
    if not pid:
        return False
    tagged = person_ids_of(hit)
    if tagged:
        return pid in tagged
    return pid in str(hit.get("content") or "")


def filter_person_hits(
    hits: Optional[Sequence[Mapping[str, Any]]],
    person_id: str,
    chat_id: str = "",
) -> List[Mapping[str, Any]]:
    """带 person 意图的过滤。失败时不退回原始 hits。

    命中该人则只返回这些人；一个都没有时，仅保留本 chat 且未标注人物的事实。
    没有 chat_id 就返回空列表。
    """
    rows = [hit for hit in (hits or []) if isinstance(hit, Mapping)]
    pid = str(person_id or "").strip()
    if not pid:
        return rows
    matched = [hit for hit in rows if hit_matches_person(hit, pid)]
    if matched:
        return matched
    scope = str(chat_id or "").strip()
    if not scope:
        return []
    kept: List[Mapping[str, Any]] = []
    for hit in rows:
        if person_ids_of(hit):
            continue
        if scope in chat_ids_of(hit):
            kept.append(hit)
    return kept


def should_block_unscoped_retry(pending_query: Optional[str], query: str, person_id: str) -> bool:
    """人物检索刚落空后，同一 query 再以空 person_id 重试 = 搜全库，必须拦住。"""
    return bool(pending_query) and pending_query == str(query or "") and not str(person_id or "").strip()


def _query_of(args: Sequence[Any], kwargs: Mapping[str, Any]) -> str:
    if args:
        return str(args[0] or "")
    return str(kwargs.get("query") or "")


def _hits_of(result: Any) -> List[Any]:
    if isinstance(result, Mapping):
        raw = result.get("hits") or []
    else:
        raw = getattr(result, "hits", None) or []
    return list(raw)


def _success_of(result: Any) -> bool:
    if isinstance(result, Mapping):
        if result.get("error"):
            return False
        if "success" in result:
            return bool(result.get("success"))
        return True
    if getattr(result, "error", ""):
        return False
    return bool(getattr(result, "success", True))


def _filtered_flag(result: Any) -> bool:
    if isinstance(result, Mapping):
        return bool(result.get("filtered"))
    return bool(getattr(result, "filtered", False))


def _as_hit_dict(hit: Any) -> Dict[str, Any]:
    if isinstance(hit, Mapping):
        return dict(hit)
    to_dict = getattr(hit, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return data
    return {"content": str(getattr(hit, "content", "") or ""), "metadata": {}}


def tighten_search_result(result: Any, person_id: str, chat_id: str = "") -> Any:
    """就地收紧一次检索结果。无法识别的结果原样返回。"""
    pid = str(person_id or "").strip()
    if not pid or result is None:
        return result
    raw_hits = _hits_of(result)
    dicts = [_as_hit_dict(hit) for hit in raw_hits]
    kept = filter_person_hits(dicts, pid, chat_id)
    if isinstance(result, dict):
        result["hits"] = kept
        if not kept:
            result["summary"] = ""
        return result
    if hasattr(result, "hits"):
        rebuilt = _rebuild_hits(raw_hits, kept)
        try:
            result.hits = rebuilt
        except Exception:
            return result
        if not rebuilt and hasattr(result, "summary"):
            try:
                result.summary = ""
            except Exception:
                pass
    return result


def _rebuild_hits(original: Sequence[Any], kept_dicts: Sequence[Mapping[str, Any]]) -> List[Any]:
    """尽量沿用原来的命中对象，避免换成上游不认识的类型。"""
    by_id = []
    for hit in original:
        by_id.append((_as_hit_dict(hit), hit))
    rebuilt: List[Any] = []
    for kept in kept_dicts:
        found = None
        for data, obj in by_id:
            if data == dict(kept) or (
                data.get("content") == kept.get("content") and (data.get("metadata") or {}) == (kept.get("metadata") or {})
            ):
                found = obj
                break
        rebuilt.append(found if found is not None else dict(kept))
    return rebuilt


def guard_search(orig: Callable[..., Any], empty_result: Callable[[], Any]) -> Callable[..., Any]:
    """包住 memory_service.search：人物未命中后的同词空 person_id 重试直接返回空。"""

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        person_id = str(kwargs.get("person_id") or "")
        query = _query_of(args, kwargs)
        chat_id = str(kwargs.get("chat_id") or "")
        pending = _PENDING_UNSCOPED.get()
        if should_block_unscoped_retry(pending[0] if pending else None, query, person_id):
            _PENDING_UNSCOPED.set(None)
            return empty_result()
        chat_token = _SEARCH_CHAT.set(chat_id)
        try:
            result = await orig(*args, **kwargs)
        finally:
            _SEARCH_CHAT.reset(chat_token)
        if not person_id:
            return result
        result = tighten_search_result(result, person_id, chat_id)
        if _success_of(result) and not _filtered_flag(result) and not _hits_of(result):
            _PENDING_UNSCOPED.set((query, person_id))
        else:
            _PENDING_UNSCOPED.set(None)
        return result

    wrapped._person_memory_guard = True  # type: ignore[attr-defined]
    return wrapped


def match_person_records(records: Iterable[Mapping[str, Any]], key: str) -> List[str]:
    """精确名字 / 昵称 / 群名片。子串不算命中。返回去重后的 person_id，保持出现顺序。"""
    needle = str(key or "").strip()
    if not needle:
        return []
    found: List[str] = []
    for row in records:
        pid = str(row.get("person_id") or "").strip()
        if not pid:
            continue
        names = [
            str(row.get("person_name") or "").strip(),
            str(row.get("user_nickname") or "").strip(),
        ]
        extra = row.get("group_nicks") or []
        if isinstance(extra, str):
            extra = [extra]
        names.extend(str(item or "").strip() for item in extra)
        if needle in names and pid not in found:
            found.append(pid)
    return found


def resolve_person_id_for_memory(
    *,
    person_name: str = "",
    platform: str = "",
    user_id: Any = None,
    records: Optional[Iterable[Mapping[str, Any]]] = None,
    strict_known: bool = False,
    is_known: Optional[Callable[[str], bool]] = None,
) -> str:
    """已有 platform+user_id 时直接用这对身份，昵称不能覆盖。

    只有没有 user_id 时才按名字查。命中 0 或 >1 都返回空字符串，不取第一条。
    strict_known 为真且该 id 尚未认识时同样返回空。
    """
    plat = str(platform or "").strip()
    uid = str(user_id or "").strip()
    if plat and uid:
        candidate = person_id_from_platform_user(plat, uid)
        if strict_known and is_known is not None and not is_known(candidate):
            return ""
        return candidate

    name = str(person_name or "").strip()
    if not name:
        return ""
    # 只按 person_name 收全量；多于 1 个就是歧义，不取第一条。
    ids = ids_by_person_name(records or [], name)
    if len(ids) != 1:
        return ""
    candidate = ids[0]
    if strict_known and is_known is not None and not is_known(candidate):
        return ""
    return candidate


def ids_by_person_name(records: Iterable[Mapping[str, Any]], person_name: str) -> List[str]:
    """只按 person_name 精确收集全部 id，供「禁止 limit(1)」使用。"""
    name = str(person_name or "").strip()
    if not name:
        return []
    found: List[str] = []
    for row in records:
        if str(row.get("person_name") or "").strip() != name:
            continue
        pid = str(row.get("person_id") or "").strip()
        if pid and pid not in found:
            found.append(pid)
    return found


def iter_ledger_members(ledger: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """按群展开，不把各群成员摊成一张以 qq 为键的表。"""
    rows: List[Dict[str, Any]] = []
    groups = ledger.get("groups") or {}
    if not isinstance(groups, Mapping):
        return rows
    for gid, group in groups.items():
        if not isinstance(group, Mapping):
            continue
        members = group.get("members") or {}
        if not isinstance(members, Mapping):
            continue
        for qq, member in members.items():
            if not isinstance(member, Mapping):
                continue
            rows.append(
                {
                    "qq": str(qq).strip(),
                    "group_id": str(gid).strip(),
                    "name": str(member.get("name") or "").strip(),
                    "score": member.get("score", 0),
                    "tier": str(member.get("tier") or "普通"),
                    "last_change": str(member.get("last_change") or "无"),
                }
            )
    return rows


def _is_qq_token(text: str) -> bool:
    return text.isdigit() and len(text) >= 5


def relation_lookup(
    ledger: Mapping[str, Any],
    who: str = "",
    *,
    user_id: str = "",
    group_id: str = "",
) -> Dict[str, Any]:
    """查关系账本。查别人时 QQ 优先于名字；重名或跨群对不上则列出 candidates。

    不会把别的群的记录悄悄当成当前群的那一位。
    """
    rows = iter_ledger_members(ledger)
    who_text = str(who or "").strip()
    uid = str(user_id or "").strip()
    gid = str(group_id or "").strip()

    if not who_text or who_text in ("自己", "我", "提问的人"):
        mine = [row for row in rows if row["qq"] == uid]
        return _scope_rows(mine, gid, status_one="self")

    if _is_qq_token(who_text):
        hits = [row for row in rows if row["qq"] == who_text]
        return _scope_rows(hits, gid, status_one="one")

    exact = [row for row in rows if row["name"] == who_text]
    hits = exact or [row for row in rows if who_text and who_text in row["name"]]
    qqs = {row["qq"] for row in hits}
    if len(qqs) > 1:
        return {"status": "ambiguous", "matches": hits}
    return _scope_rows(hits, gid, status_one="one")


def _scope_rows(hits: Sequence[Dict[str, Any]], group_id: str, *, status_one: str) -> Dict[str, Any]:
    if not hits:
        return {"status": "miss", "matches": []}
    if group_id:
        in_group = [row for row in hits if row["group_id"] == group_id]
        if len(in_group) == 1:
            return {"status": status_one, "matches": in_group}
        if len(in_group) > 1:
            return {"status": "ambiguous", "matches": in_group}
        return {"status": "ambiguous", "matches": list(hits)}
    if len(hits) == 1:
        return {"status": status_one, "matches": list(hits)}
    return {"status": "ambiguous", "matches": list(hits)}


def audit_person_ledger(
    persons: Iterable[Mapping[str, Any]],
    ledger: Mapping[str, Any],
) -> Dict[str, Any]:
    """只读对照 person_info 与 friends.json。返回报告，不修改两边。"""
    person_rows = [dict(row) for row in persons]
    members = iter_ledger_members(ledger)
    by_uid: Dict[str, List[Dict[str, Any]]] = {}
    by_name: Dict[str, List[str]] = {}
    for row in person_rows:
        uid = str(row.get("user_id") or "").strip()
        if uid:
            by_uid.setdefault(uid, []).append(row)
        name = str(row.get("person_name") or "").strip()
        pid = str(row.get("person_id") or "").strip()
        if name and pid:
            bucket = by_name.setdefault(name, [])
            if pid not in bucket:
                bucket.append(pid)

    friend_qqs = []
    for row in members:
        if row["qq"] and row["qq"] not in friend_qqs:
            friend_qqs.append(row["qq"])

    missing = [qq for qq in friend_qqs if qq not in by_uid]
    name_mismatch = []
    for row in members:
        known = by_uid.get(row["qq"]) or []
        if not known:
            continue
        names = {str(item.get("person_name") or "").strip() for item in known}
        names.discard("")
        nick = {str(item.get("user_nickname") or "").strip() for item in known}
        nick.discard("")
        if row["name"] and names and row["name"] not in names and row["name"] not in nick:
            name_mismatch.append(
                {
                    "qq": row["qq"],
                    "group_id": row["group_id"],
                    "ledger_name": row["name"],
                    "person_names": sorted(names),
                }
            )

    ambiguous_names = [
        {"person_name": name, "person_ids": ids} for name, ids in sorted(by_name.items()) if len(ids) > 1
    ]
    ledger_name_qqs: Dict[str, List[str]] = {}
    for row in members:
        if not row["name"]:
            continue
        bucket = ledger_name_qqs.setdefault(row["name"], [])
        if row["qq"] not in bucket:
            bucket.append(row["qq"])
    ambiguous_ledger = [
        {"name": name, "qqs": qqs} for name, qqs in sorted(ledger_name_qqs.items()) if len(qqs) > 1
    ]
    return {
        "person_count": len(person_rows),
        "ledger_member_rows": len(members),
        "friends_missing_in_person_info": missing,
        "name_mismatch": name_mismatch,
        "ambiguous_person_names": ambiguous_names,
        "ambiguous_ledger_names": ambiguous_ledger,
        "deleted": 0,
    }


def current_search_chat() -> str:
    return str(_SEARCH_CHAT.get() or "")


_FALLBACK_BANNER = "提示：人物定向检索未命中，已自动降级为关键词检索。\n"


def scrub_false_person_fallback(result: Any) -> Any:
    """拿掉「已降级为全库关键词」的提示，并再滤一遍 hits。

    query_memory 会在清空 person_id 重试之后固定加上那句提示。重试被拦住时
    结果是空的，这句话不能留，免得模型以为已经搜过所有人。
    """
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict) or not structured.get("fallback_applied"):
        return result
    person_id = str(structured.get("person_id") or "").strip()
    if not person_id:
        return result
    raw_hits = [hit for hit in (structured.get("hits") or []) if isinstance(hit, dict)]
    kept = [dict(hit) for hit in filter_person_hits(raw_hits, person_id, str(structured.get("chat_id") or ""))]
    structured["hits"] = kept
    structured["fallback_applied"] = False
    structured["fallback_reason"] = ""
    structured["fallback_query"] = ""
    if not kept:
        structured["summary"] = ""
        content = "未找到匹配的长期记忆。"
    else:
        lines = []
        for index, hit in enumerate(kept, start=1):
            text = " ".join(str(hit.get("content") or "").split())
            if text:
                lines.append("%d. %s" % (index, text))
        content = "\n".join(lines) if lines else "未找到匹配的长期记忆。"
    try:
        result.content = content
        result.structured_content = structured
    except Exception:
        return result
    meta = getattr(result, "metadata", None)
    if isinstance(meta, dict):
        # 这条是按降级前的 hits 生成的，滤完就不能再交给回复器。
        meta.pop("replyer_memory_reference", None)
    return result


def install() -> bool:
    """在已导入的 MaiBot 模块上挂薄包装。模块不在就跳过，重复调用无害。"""
    global _INSTALLED
    if _INSTALLED:
        return True
    import sys

    _patch_filter(sys.modules)
    _patch_search(sys.modules)
    _patch_resolve(sys.modules)
    _patch_query_memory_tool(sys.modules)
    _install_import_hook()
    _INSTALLED = True
    return True


def _patch_query_memory_tool(modules: Mapping[str, Any]) -> None:
    mod = modules.get("src.maisaka.builtin_tool")
    entries = getattr(mod, "BUILTIN_TOOL_ENTRIES", None) if mod is not None else None
    if not isinstance(entries, list):
        return
    from dataclasses import replace

    for index, entry in enumerate(list(entries)):
        if getattr(entry, "name", "") != "query_memory":
            continue
        handler = getattr(entry, "handle_tool", None)
        if handler is None or getattr(handler, "_person_memory_guard", False):
            return

        async def handle_tool(tool_ctx, invocation, context=None, _orig=handler):  # type: ignore[no-untyped-def]
            result = await _orig(tool_ctx, invocation, context)
            return scrub_false_person_fallback(result)

        handle_tool._person_memory_guard = True  # type: ignore[attr-defined]
        entries[index] = replace(entry, handle_tool=handle_tool)
        return


def _install_import_hook() -> None:
    import builtins
    import sys

    if getattr(builtins, "_person_memory_import_hook", None):
        return
    real_import = builtins.__import__

    def hooked(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        module = real_import(name, globals, locals, fromlist, level)
        try:
            _patch_filter(sys.modules)
            _patch_search(sys.modules)
            _patch_resolve(sys.modules)
            _patch_query_memory_tool(sys.modules)
        except Exception:
            pass
        return module

    builtins.__import__ = hooked  # type: ignore[assignment]
    builtins._person_memory_import_hook = hooked  # type: ignore[attr-defined]


def _patch_filter(modules: Mapping[str, Any]) -> None:
    mod = modules.get("src.A_memorix.core.runtime.services.search_hit_processing_service")
    if mod is None:
        return
    cls = getattr(mod, "MemorySearchHitProcessingService", None)
    if cls is None or getattr(cls._filter_hits, "_person_memory_guard", False):
        return
    original = cls._filter_hits

    def _filter_hits(hits, person_id):  # type: ignore[no-untyped-def]
        # 上游是 `return filtered or hits`。这里改成失败即空 / 仅本 chat 非人物事实。
        return filter_person_hits(hits, person_id, chat_id=current_search_chat())

    _filter_hits._person_memory_guard = True  # type: ignore[attr-defined]
    _filter_hits._person_memory_original = original  # type: ignore[attr-defined]
    cls._filter_hits = staticmethod(_filter_hits)


def _patch_search(modules: Mapping[str, Any]) -> None:
    mod = modules.get("src.services.memory_service")
    if mod is None:
        return
    service = getattr(mod, "memory_service", None)
    if service is None:
        return
    current = getattr(service, "search", None)
    if current is None or getattr(current, "_person_memory_guard", False):
        return

    def empty_result() -> Any:
        return mod.MemorySearchResult(summary="", hits=[], filtered=False, success=True, error="")

    service.search = guard_search(current, empty_result)

    search_mod = modules.get("src.A_memorix.core.runtime.services.memory_search_service")
    if search_mod is None:
        return
    search_cls = getattr(search_mod, "MemorySearchService", None)
    if search_cls is None or getattr(search_cls.search_memory, "_person_memory_guard", False):
        return
    orig_search = search_cls.search_memory

    async def search_memory(self, request):  # type: ignore[no-untyped-def]
        chat_id = str(getattr(request, "chat_id", "") or "")
        token = _SEARCH_CHAT.set(chat_id)
        try:
            return await orig_search(self, request)
        finally:
            _SEARCH_CHAT.reset(token)

    search_memory._person_memory_guard = True  # type: ignore[attr-defined]
    search_cls.search_memory = search_memory


def _patch_resolve(modules: Mapping[str, Any]) -> None:
    mod = modules.get("src.person_info.person_info")
    if mod is None or getattr(mod.resolve_person_id_for_memory, "_person_memory_guard", False):
        return
    original = mod.resolve_person_id_for_memory
    pure_resolve = globals()["resolve_person_id_for_memory"]

    def wrapped_resolve(  # type: ignore[no-untyped-def]
        *,
        person_name: str = "",
        platform: str = "",
        user_id: Any = None,
        strict_known: bool = False,
    ) -> str:
        known = None
        if strict_known:
            known = lambda pid: bool(mod.is_person_known(person_id=pid))
        plat = str(platform or "").strip()
        uid = str(user_id or "").strip()
        try:
            loaded = _load_person_records(mod)
        except Exception:
            loaded = None
        if loaded is None and not (plat and uid):
            # 名字这条路读不到全量就不要退回 limit(1)。
            return ""
        if plat and uid:
            loaded_for_name: List[Dict[str, Any]] = []
        else:
            name = str(person_name or "").strip()
            loaded_for_name = [
                {"person_id": pid, "person_name": name, "user_nickname": "", "group_nicks": []}
                for pid in ids_by_person_name(loaded or [], name)
            ]
        return pure_resolve(
            person_name=person_name,
            platform=platform,
            user_id=user_id,
            records=loaded_for_name,
            strict_known=strict_known,
            is_known=known,
        )

    wrapped_resolve._person_memory_guard = True  # type: ignore[attr-defined]
    wrapped_resolve._person_memory_original = original  # type: ignore[attr-defined]
    _rebind_function(modules, original, wrapped_resolve)
    mod.resolve_person_id_for_memory = wrapped_resolve

    by_name = getattr(mod, "get_person_id_by_person_name", None)
    if by_name is not None and not getattr(by_name, "_person_memory_guard", False):

        def get_person_id_by_person_name(person_name: str) -> str:  # type: ignore[no-untyped-def]
            try:
                records = _load_person_records(mod)
            except Exception:
                return ""
            if records is None:
                return ""
            ids = ids_by_person_name(records, person_name)
            return ids[0] if len(ids) == 1 else ""

        get_person_id_by_person_name._person_memory_guard = True  # type: ignore[attr-defined]
        _rebind_function(modules, by_name, get_person_id_by_person_name)
        mod.get_person_id_by_person_name = get_person_id_by_person_name

    profile_mod = modules.get("src.A_memorix.core.utils.person_profile_service")
    if profile_mod is None:
        return
    service = getattr(profile_mod, "PersonProfileService", None)
    if service is None:
        return
    orig_resolve = getattr(service, "resolve_person_id", None)
    if orig_resolve is None or getattr(orig_resolve, "_person_memory_guard", False):
        return

    def resolve_person_id(identifier: str, *, metadata_store: Any = None) -> str:
        key = str(identifier or "").strip()
        if not key:
            return ""
        try:
            records = _load_person_records(mod)
        except Exception:
            records = None
        alias_ids: List[str] = []
        if metadata_store is not None and hasattr(metadata_store, "find_person_identities"):
            try:
                found = metadata_store.find_person_identities(key)
                alias_ids = [str(item.get("person_id") or "") for item in found if str(item.get("person_id") or "")]
            except Exception:
                return ""
        if records is None:
            return ""
        ids = match_person_records(records, key)
        for pid in alias_ids:
            if pid not in ids:
                ids.append(pid)
        if len(ids) > 1:
            return ""
        if len(ids) == 1:
            return ids[0]
        if len(key) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in key):
            return key.lower()
        direct = [
            str(row.get("person_id") or "")
            for row in records
            if str(row.get("person_id") or "") == key
        ]
        if len(direct) == 1:
            return direct[0]
        return ""

    resolve_person_id._person_memory_guard = True  # type: ignore[attr-defined]
    service.resolve_person_id = staticmethod(resolve_person_id)


def _load_person_records(person_mod: Any) -> Optional[List[Dict[str, Any]]]:
    if not hasattr(person_mod, "get_db_session") or not hasattr(person_mod, "PersonInfo"):
        return None
    with person_mod.get_db_session(auto_commit=False) as session:
        rows = list(session.exec(person_mod.select(person_mod.PersonInfo)).all())
    records = []
    for row in rows:
        raw_cards = getattr(row, "group_cardname", None)
        nicks = _group_nicks(raw_cards)
        records.append(
            {
                "person_id": str(getattr(row, "person_id", "") or ""),
                "person_name": str(getattr(row, "person_name", "") or ""),
                "user_nickname": str(getattr(row, "user_nickname", "") or ""),
                "user_id": str(getattr(row, "user_id", "") or ""),
                "platform": str(getattr(row, "platform", "") or ""),
                "group_nicks": nicks,
            }
        )
    return records


def _group_nicks(raw: Any) -> List[str]:
    if not raw:
        return []
    import json

    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw)
        except Exception:
            return []
    names: List[str] = []
    if not isinstance(items, list):
        return names
    for item in items:
        if isinstance(item, dict):
            value = str(item.get("group_cardname") or item.get("group_nick_name") or "").strip()
        else:
            value = str(item or "").strip()
        if value:
            names.append(value)
    return names


def _rebind_function(modules: Mapping[str, Any], original: Any, new: Any) -> None:
    for module in list(modules.values()):
        if module is None:
            continue
        try:
            names = list(vars(module).keys())
        except Exception:
            continue
        for name in names:
            try:
                if getattr(module, name) is original:
                    setattr(module, name, new)
            except Exception:
                continue
