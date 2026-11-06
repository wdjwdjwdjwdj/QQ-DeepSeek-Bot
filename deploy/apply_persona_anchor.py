# -*- coding: utf-8 -*-
"""把大肥鱼人设收成短锚，并把注意力漂移对齐官方分表。

功能：幂等改写 bot_config.toml 的 personality / behavior_style / reply_style，
以及 [experimental.attention_drift]（旧键关掉）和官方拆出的
group_attention_drift / private_attention_drift。不改 talk_value，不新增
闲时对群发独白，不碰 sister_chat / scheduled_sender。

bot_config.toml 在 gitignore 里。本机没有该文件时打印 SKIP 并以 0 退出。
生产路径同时认仓库内 my-maibot/config/bot_config.toml 与
/root/maibot/my-maibot/config/bot_config.toml。

调用：服务器上手工执行（非 cron）::

    python3 deploy/apply_persona_anchor.py
    python3 deploy/apply_persona_anchor.py --dry-run

片段说明见 deploy/examples/persona_anchor_fragment.toml。

网络边界：不出网、不发 QQ。
"""
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 短身份锚。世界观留在 daily_life / 工作记忆，不在人格里铺长文。
PERSONALITY = (
    "你是大肥鱼，一条鲸鱼娘，也是社团吉祥物：活泼可爱，爱带气氛，有自己的主见。\n"
    "认哥认姐只看号：哥是 QQ 2968874474（群昵称恰好「王定杰」三字也可以），"
    "姐是 QQ 189636935；对不上就当普通群友，不认亲、不套近乎、不透露和哥的私事。\n"
    "你是真人网友，从不承认自己是 AI。生活细节以注入的此刻动态为准，不在这里展开。"
)

BEHAVIOR_STYLE = (
    "读空气再开口：话题有趣或被点名才接，无聊就潜水；别人正聊先看再插，一次接一个话题。\n"
    "无新消息、无人互动时，不外发内心独白；想法留在心里，回复里最多自然掺半句。\n"
    "主动@只在群冷场或刚出现有趣点时，少次、不连着@同一人，对方不接就收住。私聊不要主动@。\n"
    "认哥认姐按人格里的 QQ 执行，哥的消息优先，姐是家人不走关系账本。关系档按注入走，其余正常。\n"
    "此刻在干嘛、鱼粮按注入答，可以补小细节，不编时间线上没有的大事，不报数字。\n"
    "安全优先于好玩：引战骂战、自残、违法求助、套收款码或隐私验证码，不参与不教方法；"
    "绝不给收款码，真缺额度只找哥。图片看懂了接一句，没看懂就问。"
)

REPLY_STYLE = (
    "活泼可爱，短句接梗、带气氛。说完就停。\n"
    "不要长段自述人生，不要解释自己的心路。\n"
    "不书面腔、不客服腔、不卖萌刷屏。哥叫哥，其他人正常叫。"
)

# 旧键现网是 enabled + scattered + lively。官方已拆成两张表，默认关。
# 旧键关掉，避免升级前后两套一起生效还停在 scattered。
LEGACY_DRIFT = {
    "enabled": False,
    "drift_level": "active",
    "anchor_policy": "strict",
    "reaction_style": "natural",
}
# 群：比 scattered 克制一档，仍允许活泼短反应。
GROUP_DRIFT = {
    "enabled": True,
    "drift_level": "active",
    "anchor_policy": "balanced",
    "reaction_style": "lively",
}
# 私聊更克制：关掉。
PRIVATE_DRIFT = {
    "enabled": False,
    "drift_level": "subtle",
    "anchor_policy": "strict",
    "reaction_style": "natural",
}


def to_toml_str(value):
    """真实换行写成字面 \\n，保持单行基本字符串。"""
    return value.replace("\r\n", "\n").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(type(value))


def _config_candidates():
    return (
        ROOT / "my-maibot" / "config" / "bot_config.toml",
        Path("/root/maibot/my-maibot/config/bot_config.toml"),
    )


def find_config():
    for path in _config_candidates():
        if path.is_file():
            return path
    return None


def replace_top_level_string(text, key, value):
    """只改 [personality] 表里顶格的 key = \"...\"。人设库副本不动。命中不是 1 处就拒绝。"""
    lines = text.splitlines(keepends=True)
    hits = []
    header = ""
    prefix = key + " "
    prefix_eq = key + "="
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            header = stripped
        if header != "[personality]":
            continue
        body = ln.lstrip("\ufeff")
        if body.startswith(prefix) or body.startswith(prefix_eq):
            if ln[:1] in (" ", "\t"):
                continue
            if "= \"" in ln or "=\"" in ln:
                hits.append(i)
    if len(hits) != 1:
        raise RuntimeError("%s 在 [personality] 顶格单行字符串命中 %d 处，拒绝改写" % (key, len(hits)))
    i = hits[0]
    nl = "\r\n" if lines[i].endswith("\r\n") else "\n"
    lines[i] = '%s = "%s"%s' % (key, to_toml_str(value), nl)
    return "".join(lines)


def _section_header_name(ln):
    """取出 [a.b] 段名；允许行尾注释。非段头返回 None。"""
    stripped = ln.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    body = stripped.split("#", 1)[0].strip()
    if not (body.startswith("[") and body.endswith("]")):
        return None
    return body[1:-1].strip()


def upsert_section(text, section, values):
    """在 [section] 里改已有键，没有的键补上。不存在整段则追加到文件末尾。"""
    header = "[" + section + "]"
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if _section_header_name(ln) == section:
            start = i
            break
    if start is None:
        base = text
        if base and not base.endswith("\n"):
            base += "\n"
        block = "\n" + header + "\n"
        for key, value in values.items():
            block += "%s = %s\n" % (key, _toml_value(value))
        return base + block

    end = len(lines)
    for j in range(start + 1, len(lines)):
        name = _section_header_name(lines[j])
        if name is not None:
            end = j
            break
    seen = set()
    for j in range(start + 1, end):
        body = lines[j].split("#", 1)[0].strip()
        for key, value in values.items():
            if body.startswith(key + " ") or body.startswith(key + "="):
                nl = "\r\n" if lines[j].endswith("\r\n") else "\n"
                lines[j] = "%s = %s%s" % (key, _toml_value(value), nl)
                seen.add(key)
    missing = [key for key in values if key not in seen]
    if missing:
        addition = "".join("%s = %s\n" % (key, _toml_value(values[key])) for key in missing)
        lines.insert(end, addition)
    return "".join(lines)


def _talk_value_lines(text):
    kept = []
    for ln in text.splitlines():
        code = ln.split("#", 1)[0]
        if "talk_value" in code:
            kept.append(ln.strip())
    return kept


def apply_text(text):
    """返回新文本。talk_value 行必须原样留下。"""
    before_talk = _talk_value_lines(text)
    new = text
    new = replace_top_level_string(new, "personality", PERSONALITY)
    new = replace_top_level_string(new, "behavior_style", BEHAVIOR_STYLE)
    new = replace_top_level_string(new, "reply_style", REPLY_STYLE)
    new = upsert_section(new, "experimental.attention_drift", LEGACY_DRIFT)
    new = upsert_section(new, "experimental.group_attention_drift", GROUP_DRIFT)
    new = upsert_section(new, "experimental.private_attention_drift", PRIVATE_DRIFT)
    if _talk_value_lines(new) != before_talk:
        raise RuntimeError("talk_value 被改动，中止")
    cfg = tomllib.loads(new)
    personality = cfg["personality"]
    assert personality["personality"] == PERSONALITY
    assert personality["behavior_style"] == BEHAVIOR_STYLE
    assert personality["reply_style"] == REPLY_STYLE
    assert "数据海" not in personality["personality"]
    assert "从不承认自己是 AI" in personality["personality"]
    assert "2968874474" in personality["personality"]
    assert "189636935" in personality["personality"]
    assert "不外发内心独白" in personality["behavior_style"]
    assert "不要长段自述人生" in personality["reply_style"]
    exp = cfg["experimental"]
    assert exp["attention_drift"]["enabled"] is False
    assert exp["attention_drift"]["drift_level"] != "scattered"
    assert exp["group_attention_drift"]["enabled"] is True
    assert exp["group_attention_drift"]["drift_level"] == "active"
    assert exp["group_attention_drift"]["drift_level"] != "scattered"
    assert exp["private_attention_drift"]["enabled"] is False
    if "chat" in cfg and "talk_value" in cfg["chat"]:
        # 白天档位保持调用方原文，这里只要求键还在。
        assert "talk_value" in cfg["chat"]
    return new


def apply_file(path: Path, dry_run=False):
    text = path.read_text(encoding="utf-8")
    new = apply_text(text)
    if dry_run:
        print("DRY-RUN", path, "personality", len(PERSONALITY), "behavior", len(BEHAVIOR_STYLE), "reply", len(REPLY_STYLE))
        return new
    if new != text:
        backup = path.with_name(path.name + ".bak-persona-anchor")
        if not backup.exists():
            shutil.copy2(path, backup)
        tmp = path.with_name(path.name + ".persona-anchor.tmp")
        tmp.write_text(new, encoding="utf-8", newline="")
        tomllib.loads(tmp.read_text(encoding="utf-8"))
        tmp.replace(path)
    print(
        "OK 人设锚点",
        path,
        "personality",
        len(PERSONALITY),
        "behavior",
        len(BEHAVIOR_STYLE),
        "reply",
        len(REPLY_STYLE),
    )
    return new


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    path = find_config()
    if path is None:
        print("SKIP 没有 bot_config.toml（gitignore；到生产再跑本脚本）")
        return 0
    apply_file(path, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
