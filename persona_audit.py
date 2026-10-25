# -*- coding: utf-8 -*-
"""persona_audit.py — E4 人设漂移监测（策划案第一个工程）

每日凌晨抽样大肥鱼当日回复 → 智谱免费模型当 judge 对照人格卡四维打分 → 报告发哥。
四维：傲娇度 / 软萌度 / 禁项违例 / 记忆连贯（1-10 分）。
成本：20 次短调用全部走智谱免费 Flash；报告走本机 NapCat。
计划任务：MaiBotPersonaAudit，每日 03:00。
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
DB = BASE / "my-maibot" / "data" / "MaiBot.db"
BOT_CONFIG = BASE / "my-maibot" / "config" / "bot_config.toml"

BOT_QQ = "2365712359"      # 大肥鱼
OWNER_QQ = 2968874474      # 哥（收报告）
SAMPLE_N = 20              # 抽样条数

ALLOWED = {
    "open.bigmodel.cn": {"https"},   # 智谱 judge（免费 Flash）
    "127.0.0.1": {"http"},           # 本机 NapCat
}
JUDGE_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def http_json(url, data=None, headers=None):
    return botcommon.http_json(url, ALLOWED, data=data, headers=headers, timeout=60)


def load_persona():
    import tomllib
    cfg = tomllib.load(open(BOT_CONFIG, "rb"))
    p = cfg["personality"]
    return p["personality"][:600], p["reply_style"][:400]


def fetch_samples():
    """取她当日最近的回复，每条带上文 2 条他人消息"""
    con = sqlite3.connect(DB)
    since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute(
        "SELECT id, user_id, processed_plain_text FROM mai_messages "
        "WHERE timestamp >= ? AND processed_plain_text IS NOT NULL AND processed_plain_text != '' "
        "ORDER BY id DESC LIMIT 200",
        (since,),
    ).fetchall()
    con.close()
    bot_rows = [r for r in rows if str(r[1]) == BOT_QQ][:SAMPLE_N]
    if len(bot_rows) < 3:
        return None, 0
    samples = []
    for bot_id, _, text in bot_rows:
        con = sqlite3.connect(DB)
        ctx = con.execute(
            "SELECT processed_plain_text FROM mai_messages WHERE id < ? AND user_id != ? "
            "AND processed_plain_text != '' ORDER BY id DESC LIMIT 2",
            (bot_id, int(BOT_QQ)),
        ).fetchall()
        con.close()
        ctx_text = " | ".join(c[0][:80] for c in reversed(ctx))
        samples.append({"context": ctx_text[:180], "reply": text[:200]})
    return samples, len(rows)


def judge(samples, key, persona, style):
    convo = "\n".join(
        f"[{i+1}] 别人说: {s['context']}\n    她回复: {s['reply']}" for i, s in enumerate(samples)
    )
    prompt = (
        "你是人设一致性审查员。以下是角色\"大肥鱼\"今天的实际回复采样。\n\n"
        f"【人设卡】{persona}\n【说话风格】{style}\n\n"
        f"【今日采样】\n{convo}\n\n"
        "按人设对照打分（1-10 整数）并各给一句依据，输出严格 JSON：\n"
        '{"tsundere": {"score": 0, "note": "傲娇度：嘴硬心软/炸毛犟嘴/被夸嘴硬"},\n'
        ' "cute": {"score": 0, "note": "软萌度：语气软/语气词叠词/撒娇"},\n'
        ' "violations": {"score": 0, "note": "禁项：客服腔/毒舌讽刺/脏话/书面语（10=零违例）"},\n'
        ' "coherence": {"score": 0, "note": "记忆连贯：称呼/语境/上下文一致性"},\n'
        ' "summary": "一句话总评"}\n'
        "只输出 JSON。"
    )
    body = json.dumps({
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }).encode("utf-8")
    res = http_json(JUDGE_URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key,
    })
    content = res["choices"][0]["message"]["content"]
    # 容错：剥可能的 markdown 代码块
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(content)


def send_qq(text):
    try:
        botcommon.napcat_send_private(OWNER_QQ, text)
    except Exception as e:
        print("发送失败:", str(e)[:120])


def main():
    force = "--test" in sys.argv
    samples, total = fetch_samples()
    if not samples:
        print("近24小时她的回复不足 3 条，跳过审查")
        if not force:
            return
        samples = [{"context": "哥：在吗", "reply": "嗯呢哥~怎么啦"}] * 3  # 测试兜底
    persona, style = load_persona()
    key = botcommon.load_provider_key("Zhipu")
    t0 = time.time()
    result = judge(samples, key, persona, style)
    dt = time.time() - t0
    dims = ["tsundere", "cute", "violations", "coherence"]
    avg = sum(result[d]["score"] for d in dims) / 4
    report = (
        f"【大肥鱼·每日人设审查】{datetime.now().strftime('%m-%d')}\n"
        f"抽样 {len(samples)} 条（近24h共{total}条）\n"
        f"傲娇度 {result['tsundere']['score']}/10 ｜ 软萌度 {result['cute']['score']}/10\n"
        f"禁项守护 {result['violations']['score']}/10 ｜ 记忆连贯 {result['coherence']['score']}/10\n"
        f"综合 {avg:.1f}/10\n"
        f"总评：{result['summary']}\n"
        f"(judge: glm-5.3-flash 免费, 耗时{dt:.0f}s)"
    )
    print(report)
    send_qq(report)


if __name__ == "__main__":
    main()
