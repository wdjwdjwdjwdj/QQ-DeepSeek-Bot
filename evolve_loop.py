# -*- coding: utf-8 -*-
"""evolve_loop.py — 大肥鱼空闲自进化 loop（提案制，不自动改人设）

功能：空闲时（默认距最近人类消息 ≥60 分钟）读取近期 memory_notes / cognition_state /
friends.json 摘要，走 botcommon.llm_free_chat（allow_paid=False，绝不烧 DeepSeek）
蒸馏「可复用信念 / 禁踩坑 / 待观察」，只写入 data/evolve_proposals/YYYY-MM-DD.md。
**绝不**自动改 personality / bot_config.toml；哥审批后再人工落地。

计划任务：MaiBotEvolveLoop 每日 02:15（cron `15 2 * * *`，日志 logs/cron_evolve.log）
用法：
  py evolve_loop.py              # 正常：idle 门控 + 免费蒸馏写提案
  py evolve_loop.py --dry-run    # 只组装 prompt 打印，不调 LLM、不写盘
  py evolve_loop.py --force      # 跳过 idle 门控（冒烟/补跑）
网络边界：仅 botcommon.llm_free_chat → 商汤/智谱免费池；本机只读 SQLite（MaiBot.db）；
不写 NapCat、不发 QQ。免费全失败写 skip 原因后 exit 0。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
DB = BASE / "my-maibot" / "data" / "MaiBot.db"
NOTES_DIR = BASE / "my-maibot" / "data" / "memory_notes"
COGNITION_FILE = BASE / "cognition_state.json"
FRIENDS_FILE = BASE / "friends.json"
OUT_DIR = BASE / "data" / "evolve_proposals"

# 大肥鱼账号（现网发信 user_id；旧号保留以免误判闲时）
BOT_QQS = {"3879824374", "2365712359"}
IDLE_MINUTES_DEFAULT = 60
NOTES_DAYS = 3
NOTES_MAX_CHARS = 4500
COGNITION_MAX_CHARS = 1800
FRIENDS_TOP_N = 12
FRIENDS_MAX_CHARS = 900
PROMPT_BUDGET_HINT = 7000  # 软上限，超了截断 notes


def log(msg: str) -> None:
    print("%s %s" % (datetime.now().strftime("%m-%d %H:%M:%S"), msg))


def latest_human_ts() -> datetime | None:
    """MaiBot.db 最近一条非 bot、非通知/命令的人类消息时间。"""
    if not DB.exists():
        return None
    con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    try:
        placeholders = ",".join(["?"] * len(BOT_QQS))
        row = con.execute(
            """SELECT timestamp FROM mai_messages
               WHERE user_id NOT IN (%s)
                 AND processed_plain_text IS NOT NULL
                 AND length(processed_plain_text) > 0
                 AND IFNULL(is_notify, 0) = 0
                 AND IFNULL(is_command, 0) = 0
               ORDER BY timestamp DESC LIMIT 1"""
            % placeholders,
            tuple(BOT_QQS),
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return None
    return datetime.strptime(str(row[0])[:19], "%Y-%m-%d %H:%M:%S")


def idle_minutes() -> float | None:
    ts = latest_human_ts()
    if ts is None:
        return None
    return (datetime.now() - ts).total_seconds() / 60.0


def load_memory_notes(max_chars: int = NOTES_MAX_CHARS) -> str:
    if not NOTES_DIR.exists():
        return "(无 memory_notes)"
    files = sorted(NOTES_DIR.glob("*.md"))
    if not files:
        return "(无 memory_notes)"
    cutoff = (datetime.now() - timedelta(days=NOTES_DAYS)).date().isoformat()
    picked = [p for p in files if p.stem >= cutoff] or files[-2:]
    chunks = []
    used = 0
    for p in picked:
        body = p.read_text(encoding="utf-8", errors="replace").strip()
        header = "### %s\n" % p.stem
        piece = header + body
        if used + len(piece) > max_chars:
            remain = max_chars - used
            if remain < 80:
                break
            chunks.append(piece[:remain] + "\n…(截断)")
            used = max_chars
            break
        chunks.append(piece)
        used += len(piece)
    return "\n\n".join(chunks) if chunks else "(无近期笔记)"


def load_cognition_summary(max_chars: int = COGNITION_MAX_CHARS) -> str:
    if not COGNITION_FILE.exists():
        return "(无 cognition_state)"
    try:
        st = json.loads(COGNITION_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return "(cognition_state 读失败: %s)" % e
    lines = []
    pad = st.get("pad")
    if isinstance(pad, list) and len(pad) >= 3:
        lines.append("PAD(Pleasure/Arousal/Dominance)=[%.2f, %.2f, %.2f]" % (
            float(pad[0]), float(pad[1]), float(pad[2])
        ))
    cause = st.get("cause")
    if cause:
        lines.append("当前情绪归因: %s" % str(cause)[:200])
    intents = st.get("intentions") or []
    open_i = [x for x in intents if isinstance(x, dict) and not x.get("done")][:6]
    if open_i:
        lines.append("未完成意图:")
        for it in open_i:
            lines.append("  - %s" % str(it.get("text", ""))[:120])
    skills = st.get("skills") or []
    if skills:
        lines.append("近期技能卡:")
        for s in skills[-3:]:
            if isinstance(s, dict):
                lines.append("  - [%s] %s" % (s.get("t", "?"), str(s.get("what", ""))[:120]))
            else:
                lines.append("  - %s" % str(s)[:120])
    day = st.get("last_contradiction_day")
    if day:
        lines.append("最近矛盾日: %s" % day)
    text = "\n".join(lines) if lines else "(cognition 空)"
    return text[:max_chars]


def load_friends_summary(top_n: int = FRIENDS_TOP_N, max_chars: int = FRIENDS_MAX_CHARS) -> str:
    if not FRIENDS_FILE.exists():
        return "(无 friends.json)"
    try:
        fr = json.loads(FRIENDS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return "(friends 读失败: %s)" % e
    rows = []
    for gid, g in (fr.get("groups") or {}).items():
        for qq, m in (g.get("members") or {}).items():
            if not isinstance(m, dict):
                continue
            score = int(m.get("score", 0) or 0)
            rows.append((score, m.get("tier", ""), m.get("name", ""), qq, gid))
    rows.sort(key=lambda x: (-x[0], x[2]))
    # 优先非零分 + 前 top_n
    nonzero = [r for r in rows if r[0] != 0]
    picked = nonzero[:top_n] if nonzero else rows[:top_n]
    if not picked:
        return "(账本无人)"
    lines = ["meta.updated=%s last_review=%s" % (
        (fr.get("meta") or {}).get("updated", "?"),
        (fr.get("meta") or {}).get("last_review", "?"),
    )]
    for score, tier, name, qq, gid in picked:
        lines.append("%+d %s %s(%s) g=%s" % (score, tier, name, qq, gid))
    text = "\n".join(lines)
    return text[:max_chars]


def build_prompt(notes: str, cognition: str, friends: str) -> str:
    return (
        "你是「大肥鱼」的夜间自我反思助手。根据近期记忆与关系账本，蒸馏出可复用的进化提案。\n"
        "约束：\n"
        "1. 只输出 Markdown，分三个二级标题：## 可复用信念 / ## 禁踩坑 / ## 待观察\n"
        "2. 每条简短具体（谁/什么/为何），不要空话；每节最多 5 条\n"
        "3. 这是提案，不是立刻生效的人格改写；不要改写整段人设卡\n"
        "4. 用第三人称写大肥鱼（她），中文\n\n"
        "【近期记忆笔记】\n%s\n\n"
        "【认知状态摘要】\n%s\n\n"
        "【关系账本摘要】\n%s\n"
    ) % (notes, cognition, friends)


def write_proposal(day: str, body: str, meta: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / ("%s.md" % day)
    header = [
        "# 大肥鱼进化提案 %s" % day,
        "",
        "- generated: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "- status: %s" % meta.get("status", "proposal"),
        "- provider: %s" % meta.get("provider", "-"),
        "- model: %s" % meta.get("model", "-"),
        "- idle_min: %s" % meta.get("idle_min", "-"),
        "- note: 仅提案，须哥审批后才可改 personality / bot_config",
        "",
    ]
    if meta.get("skip_reason"):
        header.append("- skip_reason: %s" % meta["skip_reason"])
        header.append("")
    path.write_text("\n".join(header) + body.strip() + "\n", encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="大肥鱼空闲自进化 loop（提案制）")
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt，不调 LLM、不写盘")
    ap.add_argument("--force", action="store_true", help="跳过 idle 门控")
    ap.add_argument("--idle-min", type=int, default=IDLE_MINUTES_DEFAULT, help="idle 阈值分钟，默认 60")
    args = ap.parse_args()

    idle = idle_minutes()
    if not args.force and not args.dry_run:
        if idle is None:
            log("SKIP idle: 库中无人类消息可判定")
            write_proposal(
                datetime.now().strftime("%Y-%m-%d"),
                "(无提案：无法判定 idle)",
                {"status": "skip", "skip_reason": "no_human_message", "idle_min": "-"},
            )
            return 0
        if idle < args.idle_min:
            log("SKIP idle: 最近人类消息 %.1f 分钟前 < %d" % (idle, args.idle_min))
            return 0

    notes = load_memory_notes()
    cognition = load_cognition_summary()
    friends = load_friends_summary()
    prompt = build_prompt(notes, cognition, friends)
    day = datetime.now().strftime("%Y-%m-%d")
    idle_s = ("%.1f" % idle) if idle is not None else "unknown"

    log("inputs: notes=%d chars cognition=%d friends=%d prompt=%d idle=%s force=%s dry=%s"
        % (len(notes), len(cognition), len(friends), len(prompt), idle_s, args.force, args.dry_run))

    if args.dry_run:
        print("--- PROMPT BEGIN ---")
        print(prompt[:8000])
        if len(prompt) > 8000:
            print("…(prompt 共 %d chars，已截断打印)" % len(prompt))
        print("--- PROMPT END ---")
        log("DRY-RUN ok: 未调用 LLM、未写盘")
        return 0

    try:
        text, provider, model = botcommon.llm_free_chat(
            prompt, temperature=0.35, timeout=120, allow_paid=False, max_tokens=1200
        )
    except Exception as e:
        reason = str(e)[:200]
        log("SKIP free_llm: %s" % reason)
        path = write_proposal(
            day,
            "(无提案正文：免费池全失败)",
            {
                "status": "skip",
                "skip_reason": reason,
                "provider": "-",
                "model": "-",
                "idle_min": idle_s,
            },
        )
        log("wrote skip file: %s" % path)
        return 0

    path = write_proposal(
        day,
        text,
        {
            "status": "proposal",
            "provider": provider,
            "model": model,
            "idle_min": idle_s,
        },
    )
    log("OK proposal %s via %s/%s (%d chars)" % (path, provider, model, len(text)))
    log("需哥审批后才可改人设；本脚本不会自动改 personality/bot_config")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        # 非预期异常：打印后 exit 1，便于 cron 可见；不烧付费
        log("FATAL: %s" % e)
        raise SystemExit(1)
