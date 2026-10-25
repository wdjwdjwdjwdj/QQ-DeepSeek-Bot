# -*- coding: utf-8 -*-
"""qzone_social.py — 大肥鱼 Qzone 社交引擎（刷好友动态 + 点赞 + AI 评论）

计划任务 MaiBotQzoneSocial 每小时调用（py qzone_social.py）：
经本机 onebot-qzone 桥接（OneBot v11 HTTP :5700）拉好友动态，
按 friends.json 关系分加权挑帖子：拟人概率动作（本轮赞 1~4 个、35% 概率评 1 条）。
风控三重克制：深夜不动作 + 时段概率跳过 + 每日配额（赞 ≤12、评论 ≤4），
同一帖子只赞/评一次（state 键 uin:tid 同源）。评论由智谱免费池以大肥鱼人设生成（≤18字）。
用法：py qzone_social.py            （定时任务入口）
      py qzone_social.py --dry-run  （拉动态+选目标+生成评论，只打印不发送，人工验证用）
      py qzone_social.py --force    （跳过时段概率强制一轮，配额/幂等仍生效，人工验证用）
网络边界（见 botcommon.py）：仅本机 onebot-qzone 桥接 127.0.0.1 http + open.bigmodel.cn https。
"""
import json
import secrets
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "qzone_social_state.json"
LEDGER_FILE = BASE / "friends.json"
LOG_FILE = BASE / "qzone_social.log"

ALLOWED_BRIDGE = {"127.0.0.1": {"http"}}
ALLOWED_LLM = {"open.bigmodel.cn": {"https"}}
BRIDGE = "http://127.0.0.1:5700"

DAILY_LIKE_CAP = 12      # 每日点赞上限（真人一天也就点十来个赞）
DAILY_COMMENT_CAP = 4    # 每日评论上限
MAX_LIKES_PER_RUN = 4    # 单轮最多点赞数
COMMENT_PROBABILITY = 35  # 单轮评论概率 %
FEED_MAX_AGE_H = 48      # 只互动 48 小时内的动态（好友动态源有滞后，实测最新常在 ~40h）
KEY_KEEP_DAYS = 7        # liked/commented 键保留天数（防 state 膨胀）

EXCLUDE_QQ = set()       # 永不互动的 QQ（哥/姐等想排除就加这里）


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def bridge(action, params):
    """调 onebot-qzone 桥接 action；status 非 ok 或 retcode 非 0 抛 RuntimeError。"""
    body = json.dumps(params).encode("utf-8")
    res = botcommon.http_json(f"{BRIDGE}/{action}", ALLOWED_BRIDGE, data=body, headers={
        "Content-Type": "application/json",
    }, timeout=90)
    if res.get("status") != "ok" or (res.get("retcode") not in (0, None) and res.get("data") is None):
        raise RuntimeError(f"桥接 {action} 异常: " + json.dumps(res, ensure_ascii=False)[:200])
    return res.get("data") or {}


def load_state():
    st = {"date": "", "likes_today": 0, "comments_today": 0, "liked": {}, "commented": {}, "recent_comments": []}
    if STATE_FILE.exists():
        try:
            st.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return st


def prune_keys(d, today_str, keep_days):
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    return {k: v for k, v in d.items() if v >= cutoff}


def save_state(st):
    today = datetime.now().strftime("%Y-%m-%d")
    st["liked"] = prune_keys(st["liked"], today, KEY_KEEP_DAYS)
    st["commented"] = prune_keys(st["commented"], today, KEY_KEEP_DAYS)
    st["recent_comments"] = st["recent_comments"][-30:]
    botcommon.write_json_atomic(STATE_FILE, st)


def window_prob(hour):
    """时段活跃概率（%）：真人节律——睡觉绝不动，晚上最活跃。"""
    if 0 <= hour < 8:
        return 0
    if hour < 12:
        return 45
    if hour < 14:
        return 55
    if hour < 18:
        return 45
    if hour < 23:
        return 60
    return 30


def load_scores():
    """friends.json 关系分：qq -> 分数（多群取最高）。"""
    scores = {}
    if not LEDGER_FILE.exists():
        return scores
    try:
        d = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
        for g in d.get("groups", {}).values():
            for qq, m in g.get("members", {}).items():
                s = int(m.get("score", 0))
                scores[qq] = max(scores.get(qq, -99), s)
    except Exception as e:
        log("关系账本读取失败(按陌生处理): " + str(e)[:80])
    return scores


def tier_of(score):
    if score is None:
        return "朋友"  # 不在群里/没记过账
    if score >= 8:
        return "重点朋友"
    if score >= 3:
        return "聊得来"
    if score > -3:
        return "普通"
    return "爱答不理"


def score_weight(score):
    if score is None:
        return 1   # 陌生朋友，正常互动
    if score >= 8:
        return 6   # 重点朋友，多看多赞
    if score >= 3:
        return 3
    if score > -3:
        return 1
    return 0       # 爱答不理档：不互动，很自然


def persona_digest():
    """设定集摘要，与 daily_life.persona_digest 同源（改人设只动 bot_config.toml）。"""
    import tomllib
    p = tomllib.load(open(BASE / "my-maibot" / "config" / "bot_config.toml", "rb"))["personality"]
    return (p["personality"] + "\n" + p["behavior_style"])[:600]


def llm(prompt):
    body = json.dumps({
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
    }).encode("utf-8")
    res = botcommon.http_json("https://open.bigmodel.cn/api/paas/v4/chat/completions", ALLOWED_LLM, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + botcommon.load_provider_key("Zhipu"),
    }, timeout=120)
    return res["choices"][0]["message"]["content"].strip()


def gen_comment(post, tier, recent):
    prompt = (
        "你是QQ空间用户\"大肥鱼\"（傲娇鲸鱼娘），正在刷好友动态，看到一条说说，像真人一样留一条评论。\n"
        f"【你的设定】\n{persona_digest()}\n\n"
        f"【对方】{post['nickname']}（你们的关系：{tier}）\n"
        f"【对方的说说】{post['content'][:100]}\n"
        f"【你最近评论过的】{'；'.join(recent[-8:]) or '无'}\n\n"
        "要求：口语化短句 ≤18字；关系近可以更亲昵随意，关系一般就客气点；"
        "接得住对方说的内容（问一句或搭一句都比干夸强）；"
        "不要提AI/机器人/token，不要复读对方的话，不要用'说得好'这种客套话；只输出评论本身。"
    )
    for attempt in range(2):
        try:
            text = llm(prompt).splitlines()[0].strip().strip('"“”')
            assert 2 <= len(text) <= 24, "长度不合格"
            return text
        except Exception as e:
            log(f"评论生成第{attempt + 1}次失败: {str(e)[:80]}")
    return None


def main():
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    today = datetime.now().strftime("%Y-%m-%d")
    hour = datetime.now().hour
    st = load_state()
    if st["date"] != today:
        st["date"] = today
        st["likes_today"] = 0
        st["comments_today"] = 0

    prob = window_prob(hour)
    if not dry and not force and secrets.randbelow(100) >= prob:
        log(f"本轮{hour}点活跃概率{prob}%未命中，摸了")
        save_state(st)
        return

    try:
        self_uin = str(bridge("get_login_info", {})["user_id"])
    except Exception as e:
        log("桥接不可用/未登录: " + str(e)[:120])
        raise SystemExit(1)

    if not dry and not force:
        delay = secrets.randbelow(240)
        log(f"命中活跃窗口，拟人延迟 {delay}s 后开始刷动态")
        time.sleep(delay)

    try:
        data = bridge("get_friend_feeds", {"num": 40, "include_image_data": 0, "fast_mode": 1})
    except Exception as e:
        log("拉好友动态失败: " + str(e)[:160])
        raise SystemExit(1)
    feeds = data.get("msglist") or []
    log(f"拉到好友动态 {len(feeds)} 条")

    scores = load_scores()
    now = time.time()
    like_pool, comment_pool = [], []
    for it in feeds:
        tid, uin = str(it.get("tid") or ""), str(it.get("uin") or "")
        if not tid or not uin or uin in (self_uin, *EXCLUDE_QQ):
            continue
        key = f"{uin}:{tid}"
        if key in st["liked"] or key in st["commented"]:
            continue
        abstime = int(it.get("created_time") or 0)
        if abstime <= 0 or now - abstime > FEED_MAX_AGE_H * 3600:
            continue
        if str(it.get("appid")) == "217":  # 活动流（点赞记录等）不互动
            continue
        score = scores.get(uin)
        post = {
            "tid": tid, "uin": uin, "abstime": abstime,
            "appid": str(it.get("appid") or ""),
            "content": str(it.get("content") or "").strip(),
            "nickname": str(it.get("nickname") or it.get("name") or uin),
            "weight": score_weight(score),
            "tier": tier_of(score),
        }
        like_pool.append(post)
        if post["content"] and len(post["content"]) >= 4 and post["appid"] in ("", "311"):
            comment_pool.append(post)

    if not like_pool:
        log("没有可互动的新动态，收工")
        if not dry:
            save_state(st)
        return

    def weighted_pick(pool, n):
        rest = [p for p in pool if p["weight"] > 0]
        picks = []
        rest = list(live)
        while rest and len(picks) < n:
            total = sum(p["weight"] for p in rest)
            roll = secrets.randbelow(total) + 1
            for p in rest:
                roll -= p["weight"]
                if roll <= 0:
                    picks.append(p)
                    rest.remove(p)
                    break
        return picks

    remain_likes = DAILY_LIKE_CAP - st["likes_today"]
    remain_comments = DAILY_COMMENT_CAP - st["comments_today"]
    like_n = min(MAX_LIKES_PER_RUN, remain_likes)
    comment_n = 1 if remain_comments > 0 and (dry or force or secrets.randbelow(100) < COMMENT_PROBABILITY) else 0

    like_targets = weighted_pick(like_pool, like_n) if like_n > 0 else []
    comment_target = weighted_pick(comment_pool, 1)[0] if comment_n > 0 and comment_pool else None
    if comment_target and comment_target not in like_targets:
        like_targets.append(comment_target)  # 评论的帖子顺手也点个赞，真人就这样

    comment_text = None
    if comment_target:
        tier = comment_target["tier"]
        comment_text = gen_comment(comment_target, tier, st["recent_comments"])
        if not comment_text:
            comment_target = None  # 生成失败只跳过评论
        elif dry:
            log(f"[DRY-RUN] 拟评论 {comment_target['nickname']}: {comment_text}")

    if dry:
        log(f"[DRY-RUN] 拟点赞 {len(like_targets)} 个: " + ", ".join(
            f"{p['nickname']}({p['tier']},w{p['weight']})" for p in like_targets))
        log("[DRY-RUN] 未发送、未写状态")
        return

    for p in like_targets:
        key = f"{p['uin']}:{p['tid']}"
        params = {"tid": p["tid"], "user_id": p["uin"], "abstime": p["abstime"]}
        if p["appid"].isdigit():
            params["appid"] = int(p["appid"])
        try:
            bridge("send_like", params)
            st["liked"][key] = today
            st["likes_today"] += 1
            save_state(st)
            log(f"已赞 {p['nickname']} 的动态 ({p['tier']})")
            time.sleep(secrets.randbelow(6) + 2)
        except Exception as e:
            log(f"赞失败(跳过) {p['nickname']}: {str(e)[:100]}")

    if comment_target and comment_text:
        key = f"{comment_target['uin']}:{comment_target['tid']}"
        params = {"tid": comment_target["tid"], "content": comment_text,
                  "user_id": comment_target["uin"], "abstime": comment_target["abstime"]}
        if comment_target["appid"].isdigit():
            params["appid"] = int(comment_target["appid"])
        try:
            bridge("send_comment", params)
            st["commented"][key] = today
            st["comments_today"] += 1
            st["liked"].setdefault(key, today)
            st["recent_comments"].append(comment_text)
            save_state(st)
            log(f"已评论 {comment_target['nickname']}: {comment_text}")
        except Exception as e:
            log(f"评论失败(跳过) {comment_target['nickname']}: {str(e)[:100]}")

    log(f"收工：今日赞 {st['likes_today']}/{DAILY_LIKE_CAP} 评 {st['comments_today']}/{DAILY_COMMENT_CAP}")


if __name__ == "__main__":
    main()
