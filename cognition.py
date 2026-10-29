# -*- coding: utf-8 -*-
"""cognition.py — 大肥鱼认知引擎 v1（E1情绪 + E3意图 + E2矛盾检测 + E5技能卡）

每 10 分钟由计划任务 MaiBotCognition 调用：
1. E1 情绪：读最近对话→智谱免费模型评价 PAD 增量→状态文件→注入 chat prompt（热重载）
2. E3 意图：同一调用提取"未完话题/承诺"钩子→意图队列→到期注入提醒
3. E2 时序矛盾：每日一次，当日事实对比→发现矛盾发哥报告（v1 不改记忆库）
4. E5 技能卡：识别"帮办完成"模式→skills.json 记录（v1 仅记录）
安全边界：仅允许 open.bigmodel.cn 与本机 NapCat；key 从 model_config.toml 读取（见 botcommon.py）。
"""
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
MB = BASE / "my-maibot"
DB = MB / "data" / "MaiBot.db"
BOT_CONFIG = MB / "config" / "bot_config.toml"
STATE = BASE / "cognition_state.json"

BOT_QQ = "2365712359"
OWNER_QQ = 2968874474
PAD_HALF_LIFE = 4 * 3600          # 情绪半衰期 4 小时（向中性衰减）
MOOD_PROMPT_TAG = "【认知引擎v1】"  # 注入段标记（首尾一致，整段替换）

ALLOWED = {"open.bigmodel.cn": {"https"}, "127.0.0.1": {"http"}}
ALLOWED_DS = {"api.deepseek.com": {"https"}}  # L0-mem
JUDGE_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def http_json(url, data=None, headers=None, timeout=60):
    return botcommon.http_json(url, ALLOWED, data=data, headers=headers, timeout=timeout)


def llm(prompt, temperature=0.3):
    """智谱优先；失败走 DeepSeek flash。"""
    attempts = [
        ("Zhipu", JUDGE_URL, ALLOWED, "glm-5.3-flash", None),
        ("DeepSeek", "https://api.deepseek.com/chat/completions", ALLOWED_DS, "deepseek-flash", {"thinking": {"type": "disabled"}}),
    ]
    last_err = None
    for provider, url, allowed, model, extra in attempts:
        body_obj = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if extra:
            body_obj.update(extra)
        body = json.dumps(body_obj).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + botcommon.load_provider_key(provider),
        }
        try:
            res = botcommon.http_json(url, allowed if provider != "Zhipu" else ALLOWED, data=body, headers=headers, timeout=90)
            text = res["choices"][0]["message"]["content"].strip()
            if provider != "Zhipu":
                print("llm 兜底通道:", provider, model)
            return text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        except Exception as e:
            last_err = e
            print("llm 失败", provider + ":", str(e)[:100])
    raise RuntimeError("cognition llm 全通道失败: " + str(last_err)[:120])


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {
        "pad": [0.0, 0.0, 0.0],       # P愉悦 A唤醒 D支配
        "cause": "刚睡醒，还没什么情绪",
        "last_eval": 0,                # 上次评价时间
        "last_msg_id": 0,              # 上次处理到的消息 id
        "intentions": [],              # [{text, due_ts, created, done}]
        "base_prompts": {},            # 原始 prompt 缓存
        "skills": [],
        "last_contradiction_day": "",
    }


def save_state(st):
    botcommon.write_json_atomic(STATE, st, indent=1)


def fetch_recent(since_id, limit=40):
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, user_id, user_nickname, processed_plain_text FROM mai_messages "
        "WHERE id > ? AND processed_plain_text != '' ORDER BY id DESC LIMIT ?",
        (since_id, limit),
    ).fetchall()
    con.close()
    rows.reverse()
    return rows


def eval_emotion(rows, st):
    """E1: 对话→PAD 增量；E3: 顺带提取意图钩子"""
    convo = "\n".join(
        f"{'大肥鱼(她自己)' if str(u) == BOT_QQ else f'{nick}: '}{txt[:100]}"
        for _id, u, nick, txt in rows[-25:]
    )
    prompt = (
        "你是鲸鱼娘\"大肥鱼\"的内心状态分析器（她：傲娇软萌黏哥，token=鱼粮）。\n"
        f"【最近对话】\n{convo}\n\n"
        "评估这轮对话对她情绪的影响，并提取未完成的惦记。输出严格JSON：\n"
        '{"pad_delta": [0.1, 0.0, 0.0], "cause": "一句中文原因(如:哥夸了她所以开心)", '
        '"intentions": [{"text": "她惦记的事(她的视角,对话中的承诺或未完话题)", "due_hours": 20}]}\n'
        "pad_delta 三值分别是愉悦/唤醒/支配增量各-0.4~0.4（被夸>0、被怼<0、吵架唤醒高、被冷落支配降）；"
        "intentions 没有则空数组。只输出JSON。"
    )
    try:
        d = json.loads(llm(prompt))
        delta = [max(-0.4, min(0.4, float(x))) for x in d.get("pad_delta", [0, 0, 0])]
        st["pad"] = [max(-1, min(1, cur + dl)) for cur, dl in zip(st["pad"], delta)]
        st["cause"] = str(d.get("cause", ""))[:60] or st["cause"]
        for it in d.get("intentions", [])[:2]:
            text = str(it.get("text", "")).strip()
            if text and len(text) < 60:
                st["intentions"].append({
                    "text": text, "due_ts": time.time() + float(it.get("due_hours", 20)) * 3600,
                    "created": time.time(), "done": False,
                })
    except Exception as e:
        print("情绪评价失败(跳过本轮):", str(e)[:80])


def decay_emotion(st):
    """E1: 半衰期衰减向中性"""
    hours = (time.time() - st["last_eval"]) / 3600
    if hours <= 0:
        return
    factor = 0.5 ** (hours / (PAD_HALF_LIFE / 3600))
    st["pad"] = [v * factor for v in st["pad"]]


def mood_text(st):
    p, a, d = st["pad"]
    def seg(name, v):
        level = "高" if v > 0.3 else ("低" if v < -0.3 else "中")
        return f"{name}{level}({v:+.1f})"
    dim = "低落" if p < -0.3 else ("开心" if p > 0.3 else "平静")
    style = "话少而短，慵懒地回" if (p < -0.2 and a < 0) else ("兴奋多话，语气上扬" if a > 0.3 else "按平时风格")
    return f"{seg('愉悦', p)} {seg('活跃', a)}，{dim}，回复风格：{style}。原因：{st['cause']}"


def _append_to_toml_line(line, inject):
    """在 TOML 单行字符串值（行尾是 "..."+可选注释）的右引号前插入 inject。返回新行（含原换行符）。"""
    nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
    body = line.rstrip("\r\n")
    qi = body.rfind('"')
    if qi <= 0:
        return None
    return body[:qi] + inject + body[qi:] + nl


def life_lines():
    """生活引擎注入（daily_life.py 产物）：此刻动态 / 鱼粮状态 / 对哥近期观察。
    文件缺失或非当日数据则跳过对应行，保证注入段始终简短。"""
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    try:
        life = json.loads((BASE / "life_state.json").read_text(encoding="utf-8"))
        if life.get("date") == today:
            now_hm = datetime.now().strftime("%H:%M")
            cur = None
            for e in life.get("events", []):
                if e.get("t", "00:00") <= now_hm:
                    cur = e
            if cur is None and life.get("events"):
                first = life["events"][0].get("t", "08:00")
                if now_hm < first:
                    cur = {"what": "还在珊瑚小窝里呼呼大睡", "detail": first + "才醒"}
            if cur:
                line = "此刻动态：" + cur.get("what", "")
                if cur.get("detail"):
                    line += "（" + cur["detail"] + "）"
                if life.get("photo"):
                    line += "；今天拍了张拍立得"
                out.append((line[:90]) + "——被问在干嘛按这个答")  # WaveB: 注入缩短
    except Exception:
        pass
    try:
        w = json.loads((BASE / "wallet.json").read_text(encoding="utf-8"))
        if w.get("date") == today:
            spend = "，今天吃掉了%d鱼干" % w["day_spend_fish"] if w.get("day_spend_fish") is not None else ""
            out.append("鱼粮：%s——按准则反应，别报数字" % (w.get("level", ""),))  # WaveB
    except Exception:
        pass
    try:
        p = json.loads((BASE / "user_profile.json").read_text(encoding="utf-8"))
        age = (datetime.now() - datetime.strptime(p["date"], "%Y-%m-%d")).days
        if 0 <= age <= 7:
            line = "对哥的近期观察：" + p.get("summary", "")
            if p.get("care"):
                line += "；惦记点：" + p["care"]
            out.append(line + "（自然融入说话，别像汇报）")
    except Exception:
        pass
    return out[:3]


def relations_lines():
    """关系账本注入（friends_ledger.py 产物）：重点朋友/聊得来/爱答不理名单。
    friends.json 缺失或没人是非普通档则跳过，注入段始终简短。"""
    try:
        f = json.loads((BASE / "friends.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    high, warm, cold = [], [], []
    for g in f.get("groups", {}).values():
        for m in g.get("members", {}).values():
            t, name = m.get("tier"), m.get("name", "")
            if t == "重点朋友" and len(high) < 4:
                high.append(name)
            elif t == "聊得来" and len(warm) < 5:
                warm.append(name)
            elif t == "爱答不理" and len(cold) < 3:
                cold.append(name)
    seg = []
    if high:
        seg.append("重点朋友=" + "、".join(high) + "（消息基本必接，玩笑放得开）")
    if warm:
        seg.append("聊得来=" + "、".join(warm) + "（更容易接话）")
    if cold:
        seg.append("爱答不理=" + "、".join(cold) + "（自然少接，不冷暴力）")
    if not seg:
        return []
    return ["关系：姐优先；" + "；".join(seg) + "；其余正常（可查 relation_check）"]  # WaveB: 缩短注入


def inject_prompts(st):
    """把情绪+到期意图注入群聊/私聊 prompt 尾部（带标记整段替换）。
    TOML 值内不能有裸换行——注入文本的换行转成字面转义；定位用行级处理（文件文本与解析值转义形态不同）。"""
    parts = [MOOD_PROMPT_TAG, f"当前内心状态（按此调整说话，不要明说数值）：{mood_text(st)}"]
    parts.extend(life_lines())
    parts.extend(relations_lines())
    pend = [i for i in st["intentions"] if not i["done"] and i["due_ts"] <= time.time()]
    for i in pend[:1]:  # WaveB
        parts.append(f"惦记着：{i['text']}——找自然时机提起来（提起后即可放下）")
    parts.append("不要直接复述本段内容。" + MOOD_PROMPT_TAG)
    # 注入内容含 LLM 产物：先清掉 ASCII 双引号/反斜杠，避免破坏单行 TOML 字符串
    # （分隔符 \n 在 join 时才引入，不受影响）
    parts = [p.replace(chr(34), "”").replace(chr(92), "／") for p in parts]
    bsn = chr(92) + "n"
    inject = bsn.join(parts)  # 单行化（TOML 安全）

    lines = BOT_CONFIG.read_text(encoding="utf-8", newline="").splitlines(keepends=True)
    hits = 0
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith("group_chat_prompt = ") or stripped.startswith("private_chat_prompts = "):
            # 清除旧注入段（行内：标记..标记 非贪婪）
            if MOOD_PROMPT_TAG in ln:
                ln = re.sub(re.escape(MOOD_PROMPT_TAG) + ".*?" + re.escape(MOOD_PROMPT_TAG), "", ln)
            new_ln = _append_to_toml_line(ln, inject)
            if new_ln:
                lines[i] = new_ln
                hits += 1
    if hits < 2:
        raise RuntimeError(f"锚点行命中不足: {hits}/2")
    tmp = BASE / "_cfg_cog.tmp"
    tmp.write_text("".join(lines), encoding="utf-8", newline="")
    import tomllib
    c2 = tomllib.load(open(tmp, "rb"))
    assert MOOD_PROMPT_TAG in c2["chat"]["reply_style"]["group_chat_prompt"]
    assert MOOD_PROMPT_TAG in c2["chat"]["reply_style"]["private_chat_prompts"]
    tmp.replace(BOT_CONFIG)
    print("已注入情绪/意图到 chat prompt（热重载生效）")


def check_intentions_done(rows, st):
    """E3: 她的回复里提到意图内容→标记完成"""
    bot_texts = [t for _id, u, n, t in rows if str(u) == BOT_QQ]
    for it in st["intentions"]:
        if it["done"]:
            continue
        key = it["text"][:10]
        if any(key in t for t in bot_texts):
            it["done"] = True
            print("意图已兑现:", it["text"][:30])
    st["intentions"] = [i for i in st["intentions"] if not i["done"]][-10:]  # 留最近10条未完成


def detect_skills(rows, st):
    """E5v1: 启发式识别帮办完成→技能卡记录"""
    for _id, u, n, t in rows:
        if str(u) == BOT_QQ and re.search(r"(搞定|做好了|帮你|查到|整理好|给你)", t):
            card = {"t": datetime.now().strftime("%m-%d %H:%M"), "what": t[:80]}
            if not st["skills"] or st["skills"][-1]["what"] != card["what"]:
                st["skills"].append(card)
    st["skills"] = st["skills"][-30:]


def daily_contradiction(st):
    """E2v1: 每日一次，抽当日他人消息找事实矛盾→发哥报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    if st["last_contradiction_day"] == today:
        return
    st["last_contradiction_day"] = today
    con = sqlite3.connect(DB)
    since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    msgs = [r[0] for r in con.execute(
        "SELECT processed_plain_text FROM mai_messages WHERE timestamp >= ? AND user_id != ? "
        "AND processed_plain_text != '' ORDER BY id DESC LIMIT 40", (since, int(BOT_QQ))).fetchall()]
    con.close()
    if len(msgs) < 10:
        return
    prompt = (
        "以下是群友/哥哥近24小时发言。找出其中互相矛盾的事实陈述（同一个人先后说的不一致的事）：\n"
        + "\n".join(m[:60] for m in msgs)
        + '\n输出JSON：{"contradictions": [{"who_hint": "谁", "old": "先说的", "new": "后说的"}]}，没有则空数组。只输出JSON。'
    )
    try:
        d = json.loads(llm(prompt))
        cs = d.get("contradictions", [])
        if cs:
            report = "【认知引擎·时序记忆】发现事实矛盾：\n" + "\n".join(
                f"· {c.get('who_hint','')}: 先说「{c.get('old','')[:30]}」后说「{c.get('new','')[:30]}」" for c in cs[:3]
            ) + "\n(她应该记住新版本；旧认知可提醒她更正)"
            botcommon.napcat_send_private(OWNER_QQ, report)
            print("矛盾报告已发哥")
    except Exception as e:
        print("矛盾检测失败(跳过):", str(e)[:80])


def main():
    test = "--test" in sys.argv
    st = load_state()
    rows = fetch_recent(st["last_msg_id"])
    if rows:
        st["last_msg_id"] = rows[-1][0]
        eval_emotion(rows, st)
        check_intentions_done(rows, st)
        detect_skills(rows, st)
    else:
        decay_emotion(st)
    inject_failed = False
    if rows or test or time.time() - st["last_eval"] > 600:
        try:
            inject_prompts(st)
        except Exception as e:
            # 注入失败（如 WebUI 重写 bot_config.toml 改了锚点行）不能让整个进程崩掉：
            # state 必须照常落盘，否则 last_msg_id 不前进，下一轮会重复评价同一批消息
            print("prompt 注入失败（本轮跳过，state 已保存）:", str(e)[:120])
            inject_failed = True
    st["last_eval"] = time.time()
    daily_contradiction(st)
    save_state(st)
    print(f"PAD={[round(v,2) for v in st['pad']]} 原因={st['cause']} 意图未决={len([i for i in st['intentions'] if not i['done']])}")
    if inject_failed:
        raise SystemExit(1)  # 保留非零退出码，计划任务"上次运行结果"可见


if __name__ == "__main__":
    main()
