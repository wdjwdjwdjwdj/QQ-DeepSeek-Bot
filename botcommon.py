# -*- coding: utf-8 -*-
"""botcommon.py — 根目录计划任务脚本公共库

watchdog / token_guard / scheduled_sender / cognition / persona_audit 共用的
基础设施：白名单 HTTP、provider key 读取、JSON 原子写、NapCat 私聊发送。
业务脚本只保留各自的业务逻辑，网络与落盘约定统一在这里维护。

约定（详见项目根 AGENTS.md）：
- 出网必须走 http_json 且显式传 allowed 白名单，白名单外一律拒绝；
- 一律禁止跟随重定向；
- API key 只从 my-maibot/config/model_config.toml 读取，不硬编码、不进新文件；
- 状态文件一律 write_json_atomic 落盘（临时文件 + os.replace）。
"""
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
MODEL_CONFIG = BASE / "my-maibot" / "config" / "model_config.toml"
NAPCAT_API = "http://127.0.0.1:3002/send_private_msg"
NAPCAT_GROUP_API = "http://127.0.0.1:3002/send_group_msg"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("禁止重定向: " + newurl)


def http_json(url, allowed, data=None, headers=None, timeout=30):
    """白名单内的 HTTP JSON 请求。allowed 形如 {"127.0.0.1": {"http"}}；
    域名或协议不在白名单内抛 RuntimeError；不跟随重定向。"""
    parts = urlsplit(url)
    allowed_schemes = allowed.get(parts.hostname or "")
    if allowed_schemes is None or parts.scheme not in allowed_schemes:
        raise RuntimeError("目标不在白名单内，拒绝请求: " + url)
    req = urllib.request.Request(url, data=data, headers=headers or {})
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=timeout) as r:
        return json.load(r)


def load_provider_key(provider_name):
    import tomllib
    with open(MODEL_CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    for p in cfg.get("api_providers", []):
        if p.get("name") == provider_name:
            return p["api_key"]
    raise SystemExit("model_config.toml 中未找到 provider: " + provider_name)


def write_json_atomic(path, obj, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(tmp, path)


def http_bytes(url, allowed, timeout=60):
    """白名单内的 HTTP 二进制下载（如生图结果）。校验规则同 http_json，不跟随重定向。"""
    parts = urlsplit(url)
    allowed_schemes = allowed.get(parts.hostname or "")
    if allowed_schemes is None or parts.scheme not in allowed_schemes:
        raise RuntimeError("目标不在白名单内，拒绝请求: " + url)
    req = urllib.request.Request(url)
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def napcat_send_private(user_id, text):
    """通过本机 NapCat HTTP API 发私聊；返回值 status 非 ok 时抛 RuntimeError。"""
    body = json.dumps({"user_id": user_id, "message": text}).encode("utf-8")
    res = http_json(
        NAPCAT_API,
        {"127.0.0.1": {"http"}},
        data=body,
        headers={"Content-Type": "application/json"},
    )
    if res.get("status") != "ok":
        raise RuntimeError("NapCat 发送失败: " + json.dumps(res, ensure_ascii=False)[:200])
    return res


def napcat_send_record(audio_b64, user_id=None, group_id=None):
    """通过本机 NapCat HTTP API 发语音 record 段；私聊或群聊二选一。

    audio_b64: 不含 base64:// 前缀的纯 base64 字符串（mp3/wav 均可，冒烟已验 mp3）。
    返回值 status 非 ok 时抛 RuntimeError。
    """
    if bool(user_id) == bool(group_id):
        raise ValueError("napcat_send_record 需要且只能指定 user_id 或 group_id 之一")
    msg = [{"type": "record", "data": {"file": "base64://" + audio_b64}}]
    if group_id:
        url = NAPCAT_GROUP_API
        body = {"group_id": int(group_id), "message": msg}
    else:
        url = NAPCAT_API
        body = {"user_id": int(user_id), "message": msg}
    res = http_json(
        url,
        {"127.0.0.1": {"http"}},
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    if res.get("status") != "ok":
        raise RuntimeError("NapCat 语音发送失败: " + json.dumps(res, ensure_ascii=False)[:200])
    return res
