# -*- coding: utf-8 -*-
"""FriendLedger — 关系账本查询插件

向 MaiBot 的 LLM 注册一个工具（relation_check），让大肥鱼在聊天里被问到
"感情分/关系好坏/你是不是讨厌我"时，现场查 friends.json（friends_ledger.py 维护）
并用自己的口吻回答。查分是群互动玩法，人人可查，不限权限。

user_id / group_id 由宿主从真实消息上下文注入，不在工具参数表中声明，
因此不能被对话内容伪造（与 SchedulerManager 同一机制）。
"""
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from maibot_sdk import MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

OWNER_QQ = "2968874474"
SISTER_QQ = "189636935"
SISTER_NAME = "仿生人会爱上电子羊吗"

BASE = Path(__file__).resolve().parents[3]  # plugins/FriendLedger -> my-maibot -> QQ-DeepSeek-Bot
LEDGER_FILE = BASE / "friends.json"
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import person_memory  # noqa: E402


def _load_ledger() -> Dict[str, Any]:
    if not LEDGER_FILE.exists():
        return {}
    return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))


def _fmt(row: Dict[str, Any]) -> str:
    return "%s（QQ %s，群 %s）：感情分 %s，档位「%s」，最近变化：%s" % (
        row.get("name") or row.get("qq"),
        row.get("qq"),
        row.get("group_id") or "?",
        row.get("score", 0),
        row.get("tier", "普通"),
        row.get("last_change", "无"),
    )


def _fmt_many(rows: list) -> str:
    shown = rows[:8]
    text = "；".join(_fmt(row) for row in shown)
    extra = len(rows) - len(shown)
    if extra > 0:
        text += "；还有 %d 个未列出" % extra
    return text


class FriendLedgerPlugin(MaiBotPlugin):
    """关系账本：LLM 工具版。"""

    @Tool(
        "relation_check",
        description=(
            "查感情分（关系账本）。当有人问起你和他的感情分/亲密度/关系好坏"
            "（如「我们的感情分多少」「你是不是讨厌我」「你觉得我怎么样」）时调用。"
            "who 留空=查提问的人自己；查别人优先传 QQ，名字重了会列出候选人，不要猜。"
            "拿到结果后用你自己的口吻自然回答，可以调侃可以傲娇，不要像报账一样念数字。"
        ),
        parameters=[
            ToolParameterInfo(
                name="who",
                param_type=ToolParamType.STRING,
                description="要查谁的：留空=提问的人自己；优先传对方 QQ，也可以传名字（重名会列出候选人）",
                required=False,
            ),
        ],
    )
    async def relation_check(self, who: str = "", **kwargs: Any) -> Dict[str, Any]:
        user_id = str(kwargs.get("user_id") or "").strip()
        group_id = str(kwargs.get("group_id") or "").strip()
        who = str(who or "").strip()

        if not who or who in ("自己", "我", "提问的人"):
            if user_id == OWNER_QQ:
                return {"success": True, "content": "哥（QQ 2968874474）：不走感情分——认哥规则里你永远最亲，按认哥的方式答即可。"}
            if user_id == SISTER_QQ:
                return {"success": True, "content": "姐（QQ " + SISTER_QQ + "）：家人不走感情分——按姐的方式亲近作答，可以说「姐还用打分吗」。"}
        if who in ("姐", "我姐", "你姐") or "仿生人" in who or SISTER_NAME in who:
            return {"success": True, "content": "姐（QQ " + SISTER_QQ + "）：家人不走感情分——按姐的方式亲近作答，可以说「姐还用打分吗」。"}
        if user_id == OWNER_QQ and who in ("我", "自己"):
            return {"success": True, "content": "哥不走感情分，永远最亲。"}

        found = person_memory.relation_lookup(
            _load_ledger(), who, user_id=user_id, group_id=group_id,
        )
        status = found["status"]
        matches = found["matches"]
        if status == "miss":
            if not who or who in ("自己", "我", "提问的人"):
                return {"success": True, "content": "对方还没进你的关系账本（普通群友默认档）。用「关系档案」的口径自然回答，可以说「我们才刚认识没多久嘛」。"}
            return {"success": True, "content": "账本里没有叫「" + who + "」的人。可以说不认识或者让对方先混个脸熟。"}
        if status == "ambiguous":
            return {
                "success": True,
                "content": "名字或跨群对不上，不能暗自绑一个。候选人：" + _fmt_many(matches) + "。列给对方听，让对方说清是哪个群、哪个 QQ，不要自己挑一个。",
            }
        row = matches[0]
        tail = "。用你的口吻把档位说成人话，别直接念数字。"
        return {"success": True, "content": _fmt(row) + tail}

    async def on_load(self) -> None:
        try:
            person_memory.install()
        except Exception as exc:
            self.ctx.logger.info("person_memory 包装未挂上（跳过，不挡账本）: " + str(exc)[:120])
        self.ctx.logger.info("FriendLedger 已加载，账本文件: " + str(LEDGER_FILE))

    async def on_unload(self) -> None:
        pass

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        pass


def create_plugin() -> FriendLedgerPlugin:
    return FriendLedgerPlugin()
