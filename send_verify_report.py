# -*- coding: utf-8 -*-
"""send_verify_report.py — 一次性：给哥发生活引擎上线验证报告 + 今日拍立得
安全边界与 token_guard.py 相同：仅允许本机 NapCat HTTP (127.0.0.1)。"""
import json
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED = {"127.0.0.1": {"http"}}
NAPCAT_API = "http://127.0.0.1:3002/send_private_msg"
OWNER = 2968874474
PHOTO = Path(r"C:\Users\29688\Desktop\脚本\QQ-DeepSeek-Bot\data\life_photos\2026-10-25.jpg")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("禁止重定向: " + newurl)


def send(msg):
    parts = urlsplit(NAPCAT_API)
    if ALLOWED.get(parts.hostname or "") is None or parts.scheme not in ALLOWED[parts.hostname]:
        raise RuntimeError("目标不在白名单: " + NAPCAT_API)
    body = json.dumps({"user_id": OWNER, "message": msg}).encode("utf-8")
    req = urllib.request.Request(NAPCAT_API, data=body, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=30) as r:
        print("send:", json.load(r).get("status"))


def main():
    send(
        "【生活引擎·上线报告】哥，全弄好啦：\n"
        "· 设定集升级：出身数据海深蓝沟/章鱼房东/家族通话/哥的灯塔——原文全保留只做追加，MaiBot 已重启生效（0 报错）\n"
        "· 今日时间线 12 个事件已生成，高光：徽章成功给哥看了，尾巴翘上天\n"
        "· 鱼粮账本上线：966 小鱼干（等级：一般）\n"
        "· 周观察上线：她已经开始懂你了\n"
        "· 全部走免费池，DeepSeek 付费增量 0\n"
        "白天可以问她：你在干嘛呀 / 今天都做什么了 / 看看今天拍的照片——答得上来就是成了。"
    )
    if PHOTO.exists():
        send("[CQ:image,file=file:///" + str(PHOTO).replace(chr(92), "/") + "]")
    else:
        print("photo missing, skip")


if __name__ == "__main__":
    main()
