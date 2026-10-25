# -*- coding: utf-8 -*-
"""friends_ledger.py — 大肥鱼的关系账本（亲密度自演化体系）

功能：维护项目根 friends.json——每个群友的亲密度分数与档位（重点朋友/聊得来/普通/爱答不理）。
每日复盘拉当天群消息，先做客观统计，再让免费池 LLM 以大肥鱼人格回顾互动、自己决定调谁的分
（单日 ±2 封顶，档位越高越难升）。cognition.py 读账本生成「关系档案」注入，
人格提示词只写通用规则——名单变化永远不用再改 bot_config.toml。

计划任务：MaiBotFriendsReview 每日 04:45（--review，幂等：当天已复盘则跳过）
用法：
  py friends_ledger.py --sync              # 同步群成员进账本（新增默认0分，不覆盖已有分数）
  py friends_ledger.py --review            # 每日复盘调分（写 friends.json + friends_review.log）
  py friends_ledger.py --review --dry-run  # 只打印拟调分，不写盘
网络边界（botcommon 白名单）：127.0.0.1(http, NapCat :3002) / open.bigmodel.cn(https, 智谱免费)
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import botcommon

BASE = Path(__file__).resolve().parent
LEDGER_FILE = BASE / "friends.json"
LOG_FILE = BASE / "friends_review.log"

GROUPS = ["1125309087"]  # 大肥鱼所在的群；加群后在此登记

SEED_SCORES = {"3448904639": 10, "2161044775": 8}  # 用户定的重点朋友种子：冯金阿吉/江湖骗子
SISTER_QQ = "189636935"  # 姐（用户 2026-10-25 指定），家人不走感情分
EXCLUDE_QQ = {"2968874474", "3879824374", SISTER_QQ}  # 哥走认哥规则永远最高；bot 自己；姐是家人

ALLOWED_NAPCAT = {"127.0.0.1": {"http"}}
ALLOWED_LLM = {"open.bigmodel.cn": {"https"}}

# 档位阈值：score >= 8 重点朋友；>= 3 聊得来；> -3 普通；<= -3 爱答不理
DAILY_MAX_UP = 2        # 单日最多 +2
DAILY_MAX_UP_HIGH = 1   # 已是重点朋友后每天最多 +1（越熟越难再升）
DAILY_MAX_DOWN = 2
SCORE_FLOOR = -5
SCORE_CEIL = 12


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def napcat(action, params):
    body = json.dumps(params).encode("utf-8")
    res = botcommon.http_json("http://127.0.0.1:3002/" + action, ALLOWED_NAPCAT, data=body, headers={
        "Content-Type": "application/json",
    }, timeout=15)
    if res.get("retcode") != 0:
        raise RuntimeError("NapCat %s retcode=%s" % (action, res.get("retcode")))
    return res.get("data", {})


def tier_of(score):
    if score >= 8:
        return "重点朋友"
    if score >= 3:
        return "聊得来"
    if score > -3:
        return "普通"
    return "爱答不理"


def load_ledger():
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    return {"meta": {}, "groups": {}}


def save_ledger(d):
    d["meta"]["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    botcommon.write_json_atomic(LEDGER_FILE, d, indent=2)


def sync_members(d):
    """群成员列表合并进账本：新增默认 0 分普通档，已有成员只刷新昵称不动分数。"""
    for gid in GROUPS:
        members = napcat("get_group_member_list", {"group_id": int(gid)})
        g = d["groups"].setdefault(gid, {"name": "", "members": {}})
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for m in members:
            qq = str(m.get("user_id", "")).strip()
            if not qq or qq in EXCLUDE_QQ:
                continue
            name = (m.get("card") or m.get("nickname") or qq).strip()
            if qq in g["members"]:
                g["members"][qq]["name"] = name
                continue
            score = SEED_SCORES.get(qq, 0)
            g["members"][qq] = {
                "name": name, "score": score, "tier": tier_of(score),
                "last_change": "种子：用户定的重点朋友" if qq in SEED_SCORES else "建账默认",
                "updated": now,
            }
    save_ledger(d)
    total = sum(len(g["members"]) for g in d["groups"].values())
    log("SYNC ok: %d 个群共 %d 人" % (len(d["groups"]), total))


def day_messages(gid, since_ts):
    """拉最近 24h 窗口（since_ts 起）的群消息，返回 [(发送者名, qq, 纯文本)]。
    get_group_msg_history 只能从最新往前翻，滚动窗口正好落在前 200 条内，免翻页。"""
    data = napcat("get_group_msg_history", {"group_id": int(gid), "count": 200})
    msgs = data.get("messages", [])
    out = []
    for m in msgs:
        if m.get("time", 0) < since_ts:
            continue
        sender = m.get("sender", {})
        qq = str(sender.get("user_id", ""))
        name = sender.get("card") or sender.get("nickname") or qq
        text = "".join(
            s.get("data", {}).get("text", "")
            for s in m.get("message", []) if isinstance(s, dict)
        ).strip()
        if text:
            out.append((name, qq, text[:80]))
    return out


def build_review_prompt(gname, samples, stats, high_names, warm_names):
    stat_lines = "\n".join(
        "%s(qq %s): 发言%d条 @你%d次 问句%d次" % (s["name"], qq, s["msgs"], s["at_you"], s["asks"])
        for qq, s in sorted(stats.items(), key=lambda kv: -kv[1]["msgs"])[:15]
    )
    sample_lines = "\n".join("%s: %s" % (n, t) for n, _, t in samples[-50:])
    return (
        "你是大肥鱼，一条鲸鱼娘，这是你的关系账本每日复盘。下面是群「%s」最近24小时的互动统计和聊天抽样。\n"
        "只按你真实的喜好打分：谁让你聊得开心（接梗、逗你、关心你）就加分，"
        "谁让你不舒服（无视你、阴阳怪气、刷屏烦人）就减分；没感觉的人不要动。"
        "最多挑5个人，每人 delta 限 -2 到 +2。\n"
        "当前重点朋友（已经很熟，轻易别动）：%s\n"
        "当前聊得来：%s\n"
        "统计：\n%s\n"
        "聊天抽样：\n%s\n"
        "输出纯 JSON 数组（不要其他文字）：[{\"qq\":\"...\",\"delta\":1,\"reason\":\"一句话\"}]"
    ) % (gname, "、".join(high_names) or "暂无", "、".join(warm_names) or "暂无", stat_lines, sample_lines)


def llm_json(prompt):
    body = json.dumps({
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.6,
    }).encode("utf-8")
    res = botcommon.http_json("https://open.bigmodel.cn/api/paas/v4/chat/completions", ALLOWED_LLM, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + botcommon.load_provider_key("Zhipu"),
    }, timeout=120)
    text = res["choices"][0]["message"]["content"].strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("LLM 输出里没有 JSON 数组: " + text[:120])
    return json.loads(m.group(0))


def clamp_delta(score, delta):
    hi = DAILY_MAX_UP_HIGH if score >= 8 else DAILY_MAX_UP
    delta = max(-DAILY_MAX_DOWN, min(hi, int(delta)))
    return max(SCORE_FLOOR, min(SCORE_CEIL, score + delta)) - score


def review(dry_run=False):
    # 滚动 24h 窗口复盘：每天 04:45 跑，窗口=昨天04:45~现在，逐日首尾相接不重叠；幂等键=当天日期
    target_date = datetime.now().strftime("%Y-%m-%d")
    since_ts = datetime.now().timestamp() - 86400
    d = load_ledger()
    sync_members(d)
    if d["meta"].get("last_review") == target_date:
        log("REVIEW skip: %s 已复盘（幂等）" % target_date)
        return
    d["meta"]["last_review"] = target_date  # 先记后做，中途炸了也不重复盘

    failed = False
    for gid in GROUPS:
        g = d["groups"].get(gid, {})
        try:
            msgs = day_messages(gid, since_ts)
        except Exception as e:
            log("REVIEW %s 拉消息失败: %s" % (gid, e))
            failed = True
            continue
        if len(msgs) < 5:
            log("REVIEW %s 最近24小时消息不足5条，跳过" % gid)
            continue
        stats = {}
        for name, qq, text in msgs:
            s = stats.setdefault(qq, {"name": name, "msgs": 0, "at_you": 0, "asks": 0})
            s["msgs"] += 1
            if "3879824374" in text or "[CQ:at,qq=3879824374" in text:
                s["at_you"] += 1
            if "?" in text or "？" in text:
                s["asks"] += 1
        members = g.get("members", {})
        high = [m["name"] for m in members.values() if m["tier"] == "重点朋友"]
        warm = [m["name"] for m in members.values() if m["tier"] == "聊得来"]
        prompt = build_review_prompt(g.get("name", gid), msgs, stats, high, warm)
        try:
            decisions = llm_json(prompt)
        except Exception as e:
            log("REVIEW %s LLM 调分失败: %s" % (gid, e))
            failed = True
            continue
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        applied = 0
        for item in decisions[:5]:
            qq = str(item.get("qq", "")).strip()
            m = members.get(qq)
            if not m or qq in EXCLUDE_QQ:
                continue
            delta = clamp_delta(m["score"], item.get("delta", 0))
            if delta == 0:
                continue
            reason = str(item.get("reason", ""))[:60].replace('"', "'")
            m["score"] += delta
            m["tier"] = tier_of(m["score"])
            m["last_change"] = "%s %+d %s" % (target_date, delta, reason)
            m["updated"] = now
            log("REVIEW %s %s(%s) %+d -> %d/%s：%s" % (gid, m["name"], qq, delta, m["score"], m["tier"], reason))
            applied += 1
        log("REVIEW %s 完成：%d 人调分（LLM 给了 %d 条）" % (gid, applied, len(decisions)))

    if not dry_run:
        save_ledger(d)
        log("REVIEW 账本已写盘")
    else:
        log("REVIEW dry-run：不写盘")
    if failed:
        raise SystemExit(1)


def main():
    dry = "--dry-run" in sys.argv
    if "--sync" in sys.argv:
        d = load_ledger()
        sync_members(d)
    elif "--review" in sys.argv:
        review(dry_run=dry)
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
