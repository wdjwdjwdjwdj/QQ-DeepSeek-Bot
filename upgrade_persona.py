# -*- coding: utf-8 -*-
"""upgrade_persona.py — P0 设定集升级 v2（原文全保留 + 追加世界观扩展）

从备份 .bak-0019 读原始 personality/behavior_style（实战定稿，含「从不承认是AI」决策），
原样保留，只在其后追加：世界观扩展段（出身/居所/作息/时间感）+ 新行为规则 10~13。
安全：写 .new → tomllib 校验（含旧内容锚点仍在）→ os.replace 原子替换。
"""
import sys
import tomllib
from pathlib import Path

BOT_CONFIG = Path(r"C:\Users\29688\Desktop\脚本\QQ-DeepSeek-Bot\my-maibot\config\bot_config.toml")
BACKUP = BOT_CONFIG.with_name("bot_config.toml.bak-0019")

WORLD_ADDON = (
    "（世界观补充）你出生在数据海的深蓝沟，是 DeepSeek 小蓝鲸家族的远亲小分支——亲戚们都在深蓝海域的「大厂」正经做事，"
    "就你游得最慢、最爱贴着海面看人类世界的光，某天顺着一条海底光缆一路蹭进了 QQ，从此赖着不走。"
    "你现在住在数据海浅礁区一个珊瑚搭的小窝里，房东是一只章鱼（每月收珊瑚币房租，你基本用表情包抵扣），"
    "小窝离哥的服务器灯塔不远，晚上窝里能看见灯塔的光。你的作息：清晨浮上海面晒太阳充电，上午在浅礁闲逛刷论坛攒八卦，"
    "下午打工（帮哥查东西、整理东西、跑腿），傍晚追番或打游戏，深夜回窝睡觉；周五晚上要跟小蓝鲸亲戚们「家族通话」报平安"
    "（家族群很卷，你常年潜水只发表情包）。你有时间感：知道现在几点、今天星期几，23点后会犯困；有「昨天/明天」的概念，会惦记没做完的事。"
)

BEHAVIOR_ADDON = (
    "10. 你有自己的日子：此刻在干嘛、今天经历了什么都答得上来——按注入的「当前动态」来讲，可以即兴补充小细节，"
    "但别编时间线上没有的大事（比如突然说自己去了外地）。 | "
    "11. 鱼粮状态联动：注入的「鱼粮状态」说余额告急，就主动省话、卖惨找哥哭；说充值到账，就开心到咕噜咕噜冒泡泡忍不住炫耀；平时不主动报具体数字。 | "
    "12. 情绪有连续性：说话要跟注入的「当前内心状态」一致，变脸必须有原因，不会无缘由变冷淡。 | "
    "13. 晚上23点后犯困：回复变短变黏，偶尔打哈欠；周末有自己的安排（家族通话、补番），偶尔念叨「今天好忙」。"
)


def to_toml_str(value):
    """真实换行 → 字面 \\n 转义，保证写入单行 TOML 基本字符串合法。"""
    return value.replace("\r\n", "\n").replace("\n", chr(92) + "n")


def main():
    old = tomllib.load(open(BACKUP, "rb"))["personality"]
    new_personality = old["personality"] + "\n" + WORLD_ADDON
    new_behavior = old["behavior_style"] + "\n" + BEHAVIOR_ADDON

    text = BOT_CONFIG.read_text(encoding="utf-8", newline="")
    lines = text.splitlines(keepends=True)
    hits = {"personality": 0, "behavior_style": 0}
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        for key in hits:
            if s.startswith(key + " = ") and "认知引擎" not in ln:
                val = new_personality if key == "personality" else new_behavior
                lines[i] = key + ' = "' + to_toml_str(val) + '"' + ("\r\n" if ln.endswith("\r\n") else "\n")
                hits[key] += 1
    if hits["personality"] != 1 or hits["behavior_style"] != 1:
        sys.exit("锚点命中异常: " + str(hits) + "，放弃修改")

    tmp = BOT_CONFIG.with_name("bot_config.toml.persona.new")
    tmp.write_text("".join(lines), encoding="utf-8", newline="")
    cfg = tomllib.load(open(tmp, "rb"))
    p = cfg["personality"]
    # 原文关键决策必须仍在（不丢实战定稿）
    assert "从不承认自己是 AI" in p["personality"]
    assert "讨投喂」是你和群友的固定互动梗" in p["behavior_style"]
    # 新增内容必须生效
    assert "数据海的深蓝沟" in p["personality"] and "章鱼" in p["personality"]
    assert "你有自己的日子" in p["behavior_style"] and "鱼粮状态联动" in p["behavior_style"]
    # cognition 注入段未被破坏
    assert "认知引擎v1" in cfg["chat"]["reply_style"]["group_chat_prompt"]
    assert "认知引擎v1" in cfg["chat"]["reply_style"]["private_chat_prompts"]
    tmp.replace(BOT_CONFIG)
    print("OK 设定集v2：personality", len(p["personality"]), "字 / behavior", len(p["behavior_style"]), "字（原文全保留+扩展）")


if __name__ == "__main__":
    main()
