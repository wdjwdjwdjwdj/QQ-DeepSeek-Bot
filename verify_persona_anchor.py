# -*- coding: utf-8 -*-
"""校验人设锚点改写与工作记忆口气：不抬 talk_value，群/私场面分开。

用法：
  python3 verify_persona_anchor.py

网络边界：不出网、不写生产 bot_config.toml。
"""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import cognition

ROOT = Path(__file__).resolve().parent


def _load_apply():
    path = ROOT / "deploy" / "apply_persona_anchor.py"
    spec = importlib.util.spec_from_file_location("apply_persona_anchor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


APPLY = _load_apply()

FIXTURE = """
[personality]
personality = "旧的很长的人格，里面曾经写过数据海和章鱼房东。"
behavior_style = "旧行为，兴奋多话也会主动@所有人。"
reply_style = "旧回复，会解释心路。"

[[personality.profiles]]
name = "备用"
personality = "备份人设不要动"
behavior_style = "备份行为不要动"
reply_style = "备份回复不要动"

[chat]
talk_value = 0.4

[chat.reply_style]
group_chat_prompt = "群里原提示"
private_chat_prompts = "私聊原提示"

[experimental]
passive_replyer = false

[experimental.attention_drift]
enabled = true
drift_level = "scattered"
anchor_policy = "balanced"
reaction_style = "lively"
"""


def test_lengths_and_rules():
    assert len(APPLY.PERSONALITY) < 400, len(APPLY.PERSONALITY)
    assert len(APPLY.BEHAVIOR_STYLE) < 700, len(APPLY.BEHAVIOR_STYLE)
    assert len(APPLY.REPLY_STYLE) < 250, len(APPLY.REPLY_STYLE)
    assert "鲸鱼娘" in APPLY.PERSONALITY and "吉祥物" in APPLY.PERSONALITY
    assert "活泼可爱" in APPLY.PERSONALITY and "主见" in APPLY.PERSONALITY
    assert "2968874474" in APPLY.PERSONALITY and "189636935" in APPLY.PERSONALITY
    assert "数据海" not in APPLY.PERSONALITY
    assert "不外发内心独白" in APPLY.BEHAVIOR_STYLE
    assert "最多自然掺半句" in APPLY.BEHAVIOR_STYLE
    assert "冷场" in APPLY.BEHAVIOR_STYLE
    assert "不要长段自述人生" in APPLY.REPLY_STYLE
    assert "心路" in APPLY.REPLY_STYLE
    print(
        "OK 篇幅",
        len(APPLY.PERSONALITY),
        len(APPLY.BEHAVIOR_STYLE),
        len(APPLY.REPLY_STYLE),
    )


def test_apply_idempotent_and_keeps_talk_value():
    first = APPLY.apply_text(FIXTURE)
    second = APPLY.apply_text(first)
    assert first == second
    cfg = tomllib.loads(first)
    assert cfg["chat"]["talk_value"] == 0.4
    assert cfg["experimental"]["attention_drift"]["enabled"] is False
    assert cfg["experimental"]["attention_drift"]["drift_level"] == "active"
    assert cfg["experimental"]["group_attention_drift"]["drift_level"] == "active"
    assert cfg["experimental"]["group_attention_drift"]["reaction_style"] == "lively"
    assert cfg["experimental"]["private_attention_drift"]["enabled"] is False
    assert cfg["experimental"]["passive_replyer"] is False
    assert cfg["chat"]["reply_style"]["group_chat_prompt"] == "群里原提示"
    assert "scattered" not in first.split("[experimental.attention_drift]")[1].split("[")[0]
    # 人设库副本保持原句
    assert "备份人设不要动" in first
    print("OK apply 幂等且不改 talk_value")


def test_mood_and_scenes():
    st = {"pad": [0.2, 0.5, 0.0], "cause": "群里接了个梗", "intentions": []}
    text = cognition.mood_text(st)
    assert "活泼短句" in text and "语气轻快" in text and "不空发内心" in text
    assert "兴奋多话" not in text
    low = cognition.mood_text({"pad": [-0.4, -0.4, 0.0], "cause": "犯困", "intentions": []})
    assert "话少而短" in low

    group = "\n".join(cognition.build_working_memory_parts(st, cognition.SCENE_GROUP))
    private = "\n".join(cognition.build_working_memory_parts(st, cognition.SCENE_PRIVATE))
    assert cognition.INNER_VOICE_GUARD in group and cognition.INNER_VOICE_GUARD in private
    assert "接梗" in group and "主动@仅" in group
    assert "不要主动@" in private
    assert "主动@仅" not in private
    assert "少广播感" in private
    print("OK 工作记忆口气 / 群私分开")


def test_inject_group_private(tmp=Path("/tmp/cog_inject_verify")):
    tmp.mkdir(exist_ok=True)
    cfg = tmp / "bot_config.toml"
    cfg.write_text(
        '[chat.reply_style]\n'
        'group_chat_prompt = "群原"\n'
        'private_chat_prompts = "私原"\n'
        '[personality]\n'
        'personality = "x"\n',
        encoding="utf-8",
    )
    old_base, old_cfg = cognition.BASE, cognition.BOT_CONFIG
    cognition.BASE = tmp
    cognition.BOT_CONFIG = cfg
    try:
        cognition.inject_prompts({"pad": [0.0, 0.5, 0.0], "cause": "接梗", "intentions": []})
    finally:
        cognition.BASE = old_base
        cognition.BOT_CONFIG = old_cfg
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    group = data["chat"]["reply_style"]["group_chat_prompt"]
    private = data["chat"]["reply_style"]["private_chat_prompts"]
    assert "群原" in group and "接梗" in group and "禁止整段独白" in group
    assert "私原" in private and "不要主动@" in private and "主动@仅" not in private
    assert "兴奋多话" not in group and "兴奋多话" not in private
    print("OK inject 群/私分开")


def main():
    test_lengths_and_rules()
    test_apply_idempotent_and_keeps_talk_value()
    test_mood_and_scenes()
    test_inject_group_private()
    print("ALL verify_persona_anchor OK")


if __name__ == "__main__":
    main()
