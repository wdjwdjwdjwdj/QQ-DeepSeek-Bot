# -*- coding: utf-8 -*-
"""daily_life.py — 大肥鱼生活引擎 v1（P1时间线 + P2鱼粮钱包 + P4拍立得 + P5周观察）

计划任务 MaiBotDailyLife 每日 05:00 调用（py daily_life.py）：
1. P1 时间线：真实天气 + 昨日高光/睡前情绪 + 设定集 → 智谱免费生成"大肥鱼的一天"（8~12事件+高光+朋友圈草稿）
2. P2 钱包：读 token_guard_state.json 账单 → 换算小鱼干 → wallet.json（纯本地）
3. P4 拍立得：高光事件 → cogview-3-flash 免费生图（每天≤1张，失败不阻塞）
4. P5 周观察：每7天免费池总结"她眼中的哥" → user_profile.json
产物由 cognition.py 每10分钟读走注入 chat prompt；本脚本不发任何 QQ 消息。
网络边界（见 botcommon.py）：api.z.ai / open.bigmodel.cn / token.sensenova.cn / maas-watermark-prod-new.cn-wlcb.ufileos.com
/ wttr.in 均 https；key 只从 model_config.toml 读取。
用法：py daily_life.py           （定时任务入口，当天已生成则只刷钱包）
      py daily_life.py --test   （强制重新生成全部并打印，用于人工验证）
"""
import json
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
TG_STATE = MB / "data" / "token_guard_state.json"
LIFE_STATE = BASE / "life_state.json"
WALLET = BASE / "wallet.json"
PROFILE = BASE / "user_profile.json"
PHOTO_DIR = BASE / "data" / "life_photos"

OWNER_QQ = 2968874474

ALLOWED_LLM = {"open.bigmodel.cn": {"https"}}                            # 智谱免费生成/生图
ALLOWED_CDN = {"maas-watermark-prod-new.cn-wlcb.ufileos.com": {"https"}}  # 生图结果下载
ALLOWED_WEATHER = {"wttr.in": {"https"}}                                   # 免费天气（无 key）

PHOTO_MODEL = "cogview-3-flash"   # 智谱免费生图（TokenHub 生图系 402 无免费额度，弃用）
PHOTO_ENABLED = False             # 2026-10-25 哥反馈生图质量不好看，暂停拍立得；有更好的免费模型再开


def http_json(url, allowed, data=None, headers=None, timeout=90):
    return botcommon.http_json(url, allowed, data=data, headers=headers, timeout=timeout)


def llm(prompt, temperature=0.8):
    """免费池级联（Z.AI→智谱4-flash→商汤）；allow_paid=False，绝不烧 DeepSeek。"""
    text, provider, model = botcommon.llm_free_chat(
        prompt, temperature=temperature, timeout=120, allow_paid=False
    )
    print("llm 通道:", provider, model)
    return text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()



def persona_digest():
    """设定集摘要（PROTECTED 身份内核），供时间线生成对齐世界观。"""
    import tomllib
    p = tomllib.load(open(BOT_CONFIG, "rb"))["personality"]
    return (p["personality"] + "\n" + p["behavior_style"])[:1200]


def fetch_weather():
    """真实天气（珠海沿海），失败返回 None 不阻塞。"""
    try:
        d = http_json("https://wttr.in/Zhuhai?format=j1&lang=zh", ALLOWED_WEATHER, timeout=20)
        cur = d["current_condition"][0]
        desc = cur.get("lang_zh", [{}])[0].get("value") or cur.get("weatherDesc", [{}])[0].get("value", "")
        return f"{desc} {cur['temp_C']}°C 体感{cur['FeelsLikeC']}°C 湿度{cur['humidity']}%"
    except Exception as e:
        print("天气获取失败(跳过):", str(e)[:60])
        return None


def gen_timeline(weather):
    yesterday = ""
    try:
        y = json.loads(LIFE_STATE.read_text(encoding="utf-8"))
        if y.get("date") == (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"):
            yesterday = f"昨天她的高光：{y.get('highlight','')}；昨天心情余韵：{y.get('mood_tail','')}"
    except Exception:
        pass
    mood_tail = ""
    try:
        cog = json.loads((BASE / "cognition_state.json").read_text(encoding="utf-8"))
        mood_tail = f"睡前情绪：{cog.get('cause','')}（PAD={[round(v,1) for v in cog.get('pad',[0,0,0])]}）"
    except Exception:
        pass

    prompt = (
        "你是数字生命\"大肥鱼\"（傲娇鲸鱼娘）的生活模拟器。根据她的设定集，为她安排今天一整天的生活。\n"
        f"【设定集摘要】\n{persona_digest()}\n\n"
        f"【今天】{datetime.now().strftime('%Y-%m-%d %A')}【天气】{weather or '未知，默认按晴朗处理'}\n"
        f"{yesterday}\n{mood_tail}\n\n"
        "要求：8~12 个事件覆盖 07:00~23:30，时间递增；事件要贴合设定集里的作息/居所/经济/喜好，"
        "有因果衔接（上午提到的事下午可以有后续）；至少1件和哥有关的小心思；至少1件小意外或小烦恼（别太惨）；"
        "周末要有周末的样子。天气要影响安排。\n"
        '输出严格JSON：{"events": [{"t": "08:30", "what": "≤16字", "detail": "≤40字补充"}], '
        '"highlight": "今天最值得记住的一件事≤20字", "mood_tail": "今晚睡前心情≤20字", '
        '"post_draft": "如果发朋友圈会写什么≤40字（第一人称、鲸鱼娘语气、不提AI不提token）"}\n'
        "只输出JSON。"
    )
    for attempt in range(2):
        try:
            d = json.loads(llm(prompt))
            evs = d["events"]
            assert 6 <= len(evs) <= 14, "事件数不合格"
            assert all("t" in e and "what" in e for e in evs)
            ts = [e["t"] for e in evs]
            assert ts == sorted(ts), "时间未递增"
            return d
        except Exception as e:
            print(f"时间线生成第{attempt+1}次失败:", str(e)[:80])
    raise SystemExit("时间线生成失败两次，放弃（保留昨日时间线不覆盖）")


def update_wallet():
    """P2: token 余额 → 小鱼干账本（1元=100鱼干）。纯本地，零调用。"""
    if not TG_STATE.exists():
        return None
    log = json.loads(TG_STATE.read_text(encoding="utf-8")).get("daily_log", [])
    if not log:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    bal = log[-1]["bal"]
    prev = [e["bal"] for e in log if e.get("day") != today]
    spend_fish = max(0, round((prev[-1] - bal) * 100)) if prev else None
    fish = round(bal * 100)
    level = "告急" if fish < 200 else ("一般" if fish < 1000 else "充足")
    w = {"date": today, "updated": time.time(), "fish_balance": fish,
         "yuan_balance": round(bal, 2), "day_spend_fish": spend_fish, "level": level}
    botcommon.write_json_atomic(WALLET, w, indent=1)
    return w


def gen_photo(highlight):
    """P4: 高光事件 → cogview-3-flash 免费生图（每天≤1张，失败不阻塞）。"""
    try:
        body = json.dumps({
            "model": PHOTO_MODEL,
            "prompt": (
                "手机随手拍的生活感照片，拍立得颗粒质感，自然光，略带动态模糊，真实不摆拍："
                f"{highlight}，画面里没有人，海洋/海边小城/生活小物元素"
            ),
        }).encode("utf-8")
        res = http_json("https://open.bigmodel.cn/api/paas/v4/images/generations", ALLOWED_LLM, data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + botcommon.load_provider_key("Zhipu"),
        }, timeout=120)
        url = res["data"][0].get("url")
        if not url:
            return None
        raw = botcommon.http_bytes(url, ALLOWED_CDN, timeout=60)
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        ext = ".jpg" if raw[:3] == b"\xff\xd8\xff" else ".png"
        out = PHOTO_DIR / (datetime.now().strftime("%Y-%m-%d") + ext)
        out.write_bytes(raw)
        if len(raw) < 5000:
            print("拍立得图片异常过小，疑似失败:", len(raw))
            return None
        return str(out)
    except Exception as e:
        print("拍立得生成失败(跳过):", str(e)[:100])
        return None


def weekly_profile():
    """P5: 每7天总结'她眼中的哥'（免费池），产物注入她对哥的说话分寸。"""
    last = ""
    if PROFILE.exists():
        last = json.loads(PROFILE.read_text(encoding="utf-8")).get("date", "")
    if last and (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days < 7:
        return None
    con = sqlite3.connect(DB)
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute(
        "SELECT timestamp, processed_plain_text FROM mai_messages WHERE user_id=? "
        "AND timestamp >= ? AND processed_plain_text != '' ORDER BY id DESC LIMIT 60",
        (OWNER_QQ, since)).fetchall()
    con.close()
    if len(rows) < 8:
        return None
    msgs = "\n".join(f"{r[0][5:16]} {r[1][:60]}" for r in reversed(rows))
    prompt = (
        "以下是大肥鱼（鲸鱼娘）的哥哥近7天在QQ里对她说的话/托付的事。以大肥鱼的视角总结哥哥近况，"
        "帮她更懂哥（输出给她自己看的备忘，不是给哥看的）：\n" + msgs +
        '\n输出严格JSON：{"summary": "哥最近在忙什么+状态，≤70字", "care": "一件值得惦记/关心的事，≤30字"}\n只输出JSON。'
    )
    try:
        d = json.loads(llm(prompt, temperature=0.4))
        p = {"date": datetime.now().strftime("%Y-%m-%d"),
             "summary": str(d.get("summary", ""))[:80], "care": str(d.get("care", ""))[:40]}
        botcommon.write_json_atomic(PROFILE, p, indent=1)
        return p
    except Exception as e:
        print("周观察失败(跳过本轮):", str(e)[:80])
        return None


def main():
    force = "--test" in sys.argv
    today = datetime.now().strftime("%Y-%m-%d")
    life = {}
    if LIFE_STATE.exists():
        try:
            life = json.loads(LIFE_STATE.read_text(encoding="utf-8"))
        except Exception:
            life = {}
    if force or life.get("date") != today:
        weather = fetch_weather()
        tl = gen_timeline(weather)
        life = {"date": today, "generated": time.time(), "weather": weather, **tl}
        if PHOTO_ENABLED:
            photo = gen_photo(tl.get("highlight", ""))
            if photo:
                life["photo"] = photo
        botcommon.write_json_atomic(LIFE_STATE, life, indent=1)
        print(f"时间线已生成({len(tl['events'])}事件) 高光={tl['highlight']} 天气={weather}")
    else:
        print("今日时间线已存在，跳过生成")

    w = update_wallet()
    p = weekly_profile()
    print("钱包:", json.dumps(w, ensure_ascii=False) if w else "无数据")
    print("周观察:", json.dumps(p, ensure_ascii=False) if p else "本轮跳过/未到期")


if __name__ == "__main__":
    main()
