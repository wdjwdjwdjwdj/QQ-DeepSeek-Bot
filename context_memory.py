# -*- coding: utf-8 -*-
"""context_memory.py — 大肥鱼的上下文记忆归档（半天阈值压缩成 markdown）

功能：每到半天边界，把「上一个完整半天」的聊天记录从 MaiBot.db 汇总成一段 markdown 记忆
（客观统计 + 免费池 LLM 压缩的话题/互动/约定摘要），追加写入
my-maibot/data/memory_notes/YYYY-MM-DD.md。对应需求："每隔半天给上下文定一个上限，
到点自动压缩写进记忆"——MaiBot 本体的滚动压缩管"模型上下文"（窗口15/30 + 滚动摘要
+ A_Memorix 每36条写回），本脚本补"人类可读的半天档案"这一层。

计划任务：MaiBotContextMemory 每 12 小时（/st 00:30 → 00:30/12:30 各一次：
00:30 归档昨天下午段，12:30 归档今天上午段；手动补跑处理"上一个完整半天"）
半日 token 上限：HALF_DAY_TOKEN_CAP（llm_usage 全任务口径）。超限时在归档节标 ⚠️
并私聊哥一条告警（幂等：每半日键最多一条，发送成功才记键）；压缩本身由 MaiBot 自动做，
上限的职责=让哥看到超限并决定是否降温。
用法：
  py context_memory.py             # 归档上一个完整半天（幂等：该段已归档则跳过）
  py context_memory.py --dry-run   # 只收集打印，不调 LLM、不写盘、不写 state、不发告警
网络边界（botcommon 白名单）：open.bigmodel.cn(https, 智谱 glm-5.3-flash 免费)；
127.0.0.1(http, NapCat :3002，仅超限告警私聊哥)；本机只读 SQLite（file:...?mode=ro）。
失败语义：LLM 失败时不写盘、不推进幂等键、exit 1——窗口数据一直在 db 里，下次运行重试同窗口。
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
DB = BASE / "my-maibot" / "data" / "MaiBot.db"
NOTES_DIR = BASE / "my-maibot" / "data" / "memory_notes"
STATE_FILE = BASE / "context_memory_state.json"
LOG_FILE = BASE / "context_memory.log"

BOT_QQ = "3879824374"
GE_QQ = "2968874474"               # 哥：唯一告警接收人（与 token_guard 同号）
ALLOWED_LLM = {"open.bigmodel.cn": {"https"}}
ALLOWED_DS = {"api.deepseek.com": {"https"}}  # L0-mem: 智谱 429 走 DeepSeek
MAX_SAMPLE_CHARS = 12000   # 给 LLM 的聊天抽样总字符预算
PER_MSG_CHARS = 100        # 单条消息截断长度
HALF_DAY_TOKEN_CAP = 3_000_000  # 半日 token 上限（llm_usage 全任务口径；10-25 实测活跃日半天 195~330万）


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def half_window(now=None):
    """返回上一个完整半天的 (date_key, half, title, start, end)。
    12:00 后跑 → 今天上午段；12:00 前跑 → 昨天下午段（两个时点都保证窗口已完整闭合）。"""
    now = now or datetime.now()
    if now.hour >= 12:
        d, half = now.date(), "AM"
    else:
        d, half = (now - timedelta(days=1)).date(), "PM"
    if half == "AM":
        start = datetime(d.year, d.month, d.day, 0, 0)
        end = start + timedelta(hours=12)
        title = "上午（00:00 ~ 12:00）"
    else:
        start = datetime(d.year, d.month, d.day, 12, 0)
        end = start + timedelta(hours=12)
        title = "下午（12:00 ~ 24:00）"
    return d.isoformat(), half, title, start, end


def fetch_rows(start, end):
    """只读拉取窗口内聊天消息（排除通知/命令）。"""
    con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    try:
        return con.execute(
            """SELECT timestamp, session_id, user_id, user_nickname, user_cardname,
                      group_name, is_picture, is_emoji, processed_plain_text
               FROM mai_messages
               WHERE timestamp >= ? AND timestamp < ? AND is_notify = 0 AND is_command = 0
               ORDER BY timestamp""",
            (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
    finally:
        con.close()


def fetch_llm_usage(start, end):
    con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    try:
        row = con.execute(
            """SELECT COUNT(*), COALESCE(SUM(total_tokens), 0) FROM llm_usage
               WHERE timestamp >= ? AND timestamp < ?""",
            (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
        return row[0], row[1]
    finally:
        con.close()


def name_of(user_id, nickname, cardname):
    if user_id == BOT_QQ:
        return "大肥鱼(我)"
    return (cardname or nickname or ("qq" + str(user_id))).strip()[:20]


def build_stats(rows):
    total = len(rows)
    pictures = sum(1 for r in rows if r[6])
    emojis = sum(1 for r in rows if r[7])
    groups = {}
    speakers = {}
    for r in rows:
        if r[5]:
            groups[r[5]] = groups.get(r[5], 0) + 1
        name = name_of(r[2], r[3], r[4])
        speakers[name] = speakers.get(name, 0) + 1
    private = total - sum(groups.values())
    top = "、".join("%s %d" % (n, c) for n, c in sorted(speakers.items(), key=lambda kv: -kv[1])[:6])
    scope = "、".join("群「%s」%d" % (g, c) for g, c in groups.items())
    if private:
        scope += ("；" if scope else "") + "私聊 %d" % private
    return total, pictures, emojis, scope, top


def build_sample(rows):
    """按时间顺序拼聊天抽样行；超预算时均匀抽稀（保头尾），保留全天覆盖感。"""
    lines = []
    for r in rows:
        ts, text = r[0], (r[8] or "").strip().replace("\n", " ")
        if not text:
            text = "[图片]" if r[6] else "[表情包]" if r[7] else "（空）"
        lines.append("[%s] %s: %s" % (ts[11:16], name_of(r[2], r[3], r[4]), text[:PER_MSG_CHARS]))
    total_chars = sum(len(x) + 1 for x in lines)
    if total_chars <= MAX_SAMPLE_CHARS:
        return "\n".join(lines)
    stride = max(2, -(-total_chars // MAX_SAMPLE_CHARS))  # 均匀抽稀，保头尾，维持全天覆盖感
    sampled = lines[::stride]
    if sampled and sampled[0] != lines[0]:
        sampled[0] = lines[0]
    if sampled and sampled[-1] != lines[-1]:
        sampled[-1] = lines[-1]
    return "\n".join(sampled)


def llm_summary(title, sample):
    """智谱优先；429/失败则 DeepSeek flash 兜底。保留原归档 prompt 与节校验。"""
    prompt = (
        "你是聊天记录归档员。下面是 QQ 机器人大肥鱼「%s」经历的聊天记录抽样"
        "（[时间] 名字: 内容；[图片]/[表情包] 是媒体占位，抽稀过但时间顺序真实）。\n"
        "压缩成记忆要点，输出 markdown 正文（不要代码块包裹），严格用以下三节，每节 2~5 条短句：\n"
        "### 话题与进展\n### 人物与互动\n### 约定与未完事项\n"
        "（第三节没有可写的内容就只写「（无）」；只依据记录，不编造。）\n\n聊天抽样：\n%s"
    ) % (title, sample)
    attempts = [
        ("Zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions", ALLOWED_LLM, "glm-5.3-flash", None),
        ("DeepSeek", "https://api.deepseek.com/chat/completions", ALLOWED_DS, "deepseek-flash", {"thinking": {"type": "disabled"}}),
    ]
    last_err = None
    for provider, url, allowed, model, extra in attempts:
        body_obj = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if extra:
            body_obj.update(extra)
        body = json.dumps(body_obj).encode("utf-8")
        try:
            res = botcommon.http_json(url, allowed, data=body, headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + botcommon.load_provider_key(provider),
            }, timeout=180)
            text = res["choices"][0]["message"]["content"].strip()
            text = text.removeprefix("```markdown").removeprefix("```").removesuffix("```").strip()
            if "话题与进展" not in text:
                raise ValueError("LLM 输出缺标准节: " + text[:120])
            if provider != "Zhipu":
                log("llm 兜底通道: %s %s" % (provider, model))
            return text
        except Exception as e:
            last_err = e
            log("llm 失败 %s: %s" % (provider, str(e)[:120]))
    raise RuntimeError("llm_summary 全通道失败: " + str(last_err)[:120])


def write_section(date_key, title, section):
    """把半日节追加进当日 markdown；同名节已存在则整节替换（防部分写入残留）。"""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = NOTES_DIR / (date_key + ".md")
    header = "## %s %s" % (date_key, title)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    # 按节拆分：每节以 "## " 开头
    sections = []
    for i, block in enumerate(old.split("\n## ")):
        text = block if i == 0 else "## " + block
        sections.append(text.rstrip() + "\n")
    kept = [s for s in sections if not s.startswith(header)]
    kept.append(section.rstrip() + "\n")
    kept = sorted(kept, key=lambda s: s.split("\n")[0])  # 按节标题排序，半天自然升序
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(kept), encoding="utf-8")
    os.replace(tmp, path)
    return path


def parse_window_arg():
    """支持 --window 2026-10-28-PM 补跑漏窗；无参则走 half_window()。"""
    if "--window" not in sys.argv:
        return None
    i = sys.argv.index("--window")
    if i + 1 >= len(sys.argv):
        raise SystemExit("用法: --window YYYY-MM-DD-AM|PM")
    key = sys.argv[i + 1].strip().upper()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})-(AM|PM)$", key)
    if not m:
        raise SystemExit("窗口格式错误: " + key)
    date_key, half = m.group(1), m.group(2)
    day = datetime.strptime(date_key, "%Y-%m-%d")
    if half == "AM":
        start = day.replace(hour=0, minute=0, second=0)
        end = day.replace(hour=12, minute=0, second=0)
        title = "上午（00:00 ~ 12:00）"
    else:
        start = day.replace(hour=12, minute=0, second=0)
        end = day.replace(hour=0, minute=0, second=0) + __import__("datetime").timedelta(days=1)
        title = "下午（12:00 ~ 24:00）"
    return date_key, half, title, start, end


def main():
    dry = "--dry-run" in sys.argv
    override = parse_window_arg()
    if override:
        date_key, half, title, start, end = override
    else:
        date_key, half, title, start, end = half_window()
    idem_key = "%s-%s" % (date_key, half)
    rows = fetch_rows(start, end)
    total, pictures, emojis, scope, top = build_stats(rows)
    calls, tokens = fetch_llm_usage(start, end)
    log("窗口 %s %s：%d 条消息（图%d/表情%d）LLM %d 次/%.1f万token"
        % (idem_key, title, total, pictures, emojis, calls, tokens / 10000))

    if total == 0:
        log("窗口无消息，跳过（不记幂等键，防止空段写盘）")
        return
    if not dry:
        st = {}
        if STATE_FILE.exists():
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if idem_key in st.get("done", {}):
            log("SKIP: %s 已归档（幂等）" % idem_key)
            return

    over = tokens >= HALF_DAY_TOKEN_CAP
    sample = build_sample(rows)
    if dry:
        print("半日 token %.1f 万（上限 %.0f 万，%s）" % (
            tokens / 10000, HALF_DAY_TOKEN_CAP / 10000, "将超限告警" if over else "未超"))
        print("---- 抽样(%d字) ----\n%s\n---- 拟写文件: %s" %
              (len(sample), sample[:800], NOTES_DIR / (date_key + ".md")))
        return

    try:
        summary = llm_summary(title, sample)
    except Exception as e:
        log("LLM 压缩失败: %r（不写盘不推进键，下次重试同窗口）" % e)
        raise SystemExit(1)

    usage = "LLM 调用 %d 次 / %.1f万 token" % (calls, tokens / 10000) if calls else "LLM 无记录"
    if over:
        usage += "｜⚠️ 超半天上限 %.0f 万" % (HALF_DAY_TOKEN_CAP / 10000)
    section = (
        "## %s %s\n"
        "- 统计：消息 %d 条（%s）｜图片 %d｜表情包 %d｜%s\n"
        "- 活跃：%s\n\n%s\n"
    ) % (date_key, title, total, scope, pictures, emojis, usage, top or "（无人发言）", summary)
    path = write_section(date_key, title, section)

    st = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    st.setdefault("done", {})[idem_key] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    botcommon.write_json_atomic(STATE_FILE, st)
    log("OK: %s 已写入 %s（抽样 %d 字 → 摘要 %d 字）" % (idem_key, path.name, len(sample), len(summary)))

    if over and idem_key not in st.get("alerts", {}):
        text = ("哥，跟你报个账：刚过去的半天（%s %s）token 烧了 %.0f 万，超过半天上限 %.0f 万了。"
                "大头是群里热闹——每条消息都要过 planner/压缩/记忆抽取这几道，压缩和半天归档都正常在跑。"
                "明细在 my-maibot/data/memory_notes/，想降温你说一声，我把说话频率调低。"
                ) % (date_key, title, tokens / 10000, HALF_DAY_TOKEN_CAP / 10000)
        try:
            botcommon.napcat_send_private(GE_QQ, text)
            st.setdefault("alerts", {})[idem_key] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            botcommon.write_json_atomic(STATE_FILE, st)
            log("超限告警已私聊哥（%.0f万 ≥ 上限%.0f万）" % (tokens / 10000, HALF_DAY_TOKEN_CAP / 10000))
        except Exception as e:
            log("告警发送失败: %r（alerts 未记键，同窗口手动重跑可补发）" % e)


if __name__ == "__main__":
    main()
