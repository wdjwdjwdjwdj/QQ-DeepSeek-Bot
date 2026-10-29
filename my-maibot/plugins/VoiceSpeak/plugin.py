# -*- coding: utf-8 -*-
"""VoiceSpeak — 语音发送插件

向 MaiBot 的 LLM 注册工具 speak_voice：用户要听语音/发语音时，
合成中文女声并经 NapCat 发到当前私聊或群聊。

user_id / group_id 由宿主从真实消息上下文注入，不能被对话伪造。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

from maibot_sdk import MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

BASE = Path(__file__).resolve().parents[3]  # plugins/VoiceSpeak -> my-maibot -> maibot
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from voice_tts import DEFAULT_VOICE, speak  # noqa: E402


class VoiceSpeakPlugin(MaiBotPlugin):
    """edge-tts + NapCat 发语音。"""

    @Tool(
        "speak_voice",
        description=(
            "发一条 QQ 语音消息。当对方明确要求听语音/发语音/语音说/念出来"
            "（如「发语音」「用语音说」「念给我听」），或你想用语音表达短句时调用。"
            "text 填要说出口的话（口语短句，建议不超过 80 字）。"
            "调用成功后只用一句话确认（如「发了」），不要把全文再打成文字。"
        ),
        parameters=[
            ToolParameterInfo(
                name="text",
                param_type=ToolParamType.STRING,
                description="要念出来的中文口语短句",
                required=True,
            ),
        ],
    )
    async def speak_voice(self, text: str = "", **kwargs: Any) -> Dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            return {"success": False, "content": "没有要说的内容，请先想好短句再发语音。"}
        if len(text) > 500:
            return {"success": False, "content": "太长了，语音请控制在 500 字以内，再短一点。"}

        group_id = str(kwargs.get("group_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()
        if not group_id and not user_id:
            return {"success": False, "content": "拿不到聊天对象，语音发不出去。"}

        try:
            _b64, res = speak(
                text,
                user_id=None if group_id else user_id,
                group_id=group_id or None,
                voice=DEFAULT_VOICE,
            )
        except Exception as e:
            self.ctx.logger.error("speak_voice failed: %s", e)
            return {"success": False, "content": "语音合成或发送失败：" + str(e)[:160]}

        mid = None
        if isinstance(res, dict):
            mid = (res.get("data") or {}).get("message_id")
        self.ctx.logger.info(
            "speak_voice ok mid=%s group=%s user=%s chars=%s",
            mid,
            group_id or "-",
            user_id or "-",
            len(text),
        )
        return {
            "success": True,
            "content": "语音已发出"
            + (("（message_id=" + str(mid) + "）") if mid else "")
            + "。用一句话确认即可，不要重复念全文。",
        }

    async def on_load(self) -> None:
        self.ctx.logger.info("VoiceSpeak loaded (edge-tts → NapCat record)")

    async def on_unload(self) -> None:
        pass

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        pass


def create_plugin() -> VoiceSpeakPlugin:
    return VoiceSpeakPlugin()
