# -*- coding: utf-8 -*-
"""voice_tts.py — edge-tts 合成 + NapCat 发语音

功能：把文本合成中文女声 mp3，再经本机 NapCat HTTP 发成 QQ 语音。
调用方：VoiceSpeak 插件（聊天里 LLM 工具）；也可 CLI 冒烟。
依赖：/root/maibot/.venv-tts（edge-tts），标准库 subprocess；网络走 botcommon。
用法：
  python3 voice_tts.py --text "你好" --user-id 2968874474
  python3 voice_tts.py --text "群测" --group-id 123456 --dry-run
音色：默认晓晓 -8%/+3Hz（软A，用户选定）；edge-tts 免费。可选 FEIYU_TTS_URL。
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from botcommon import napcat_send_record

BASE = Path(__file__).resolve().parent
EDGE_TTS = BASE / ".venv-tts" / "bin" / "edge-tts"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_RATE = os.environ.get("MAIBOT_TTS_RATE", "-8%")
DEFAULT_PITCH = os.environ.get("MAIBOT_TTS_PITCH", "+3Hz")
FEIYU_TTS_URL = os.environ.get("FEIYU_TTS_URL", "").rstrip("/")
FEIYU_TTS_KEY = os.environ.get("FEIYU_TTS_KEY", "")
FEIYU_STYLE = os.environ.get("FEIYU_TTS_STYLE", "cute")


def _synthesize_feiyu(text: str) -> bytes:
    """调用本机/隧道上的肥鱼声音服务（CosyVoice3 cute/gentle）。"""
    import json
    import time
    import urllib.error
    import urllib.request

    if not FEIYU_TTS_URL:
        raise RuntimeError("未配置 FEIYU_TTS_URL")
    payload = json.dumps(
        {"text": text, "voice": "feiyu", "style": FEIYU_STYLE},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if FEIYU_TTS_KEY:
        headers["Authorization"] = "Bearer " + FEIYU_TTS_KEY

    def _req(method: str, path: str, data: bytes | None = None) -> bytes:
        r = urllib.request.Request(
            FEIYU_TTS_URL + path, data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.read()

    try:
        row = json.loads(_req("POST", "/v1/speech", payload).decode())
        job_id = row.get("id") or row.get("job_id")
        if not job_id:
            raise RuntimeError("肥鱼服务未返回 job id: " + str(row)[:200])
        for _ in range(60):
            st = json.loads(_req("GET", f"/v1/jobs/{job_id}").decode())
            status = (st.get("status") or st.get("state") or "").lower()
            if status in ("done", "completed", "success", "succeeded"):
                audio = _req("GET", f"/v1/jobs/{job_id}/audio")
                if not audio:
                    raise RuntimeError("肥鱼音频为空")
                return audio
            if status in ("failed", "error"):
                raise RuntimeError("肥鱼合成失败: " + str(st)[:300])
            time.sleep(1)
        raise RuntimeError("肥鱼合成超时")
    except urllib.error.HTTPError as e:
        if e.code not in (404, 405):
            raise RuntimeError(f"肥鱼服务 HTTP {e.code}: {e.read()[:200]!r}") from e
    audio = _req("POST", "/v1/tts", payload)
    if not audio:
        raise RuntimeError("肥鱼同步 TTS 产出空")
    return audio


def synthesize_mp3(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    """合成音频字节；若配置了 FEIYU_TTS_URL 则走真肥鱼声，否则 edge-tts。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("合成文本为空")
    if len(text) > 500:
        raise ValueError("合成文本过长（上限 500 字）")
    if FEIYU_TTS_URL:
        return _synthesize_feiyu(text)
    if not EDGE_TTS.exists():
        raise RuntimeError("未找到 edge-tts：请先建 /root/maibot/.venv-tts 并 pip install edge-tts")
    with tempfile.TemporaryDirectory(prefix="maibot-tts-") as td:
        out = Path(td) / "out.mp3"
        # rate/pitch 必须写成 --rate=-5% 这种形式，否则 argparse 会把 -5% 当新选项
        cmd = [
            str(EDGE_TTS),
            "--voice=" + voice,
            "--rate=" + DEFAULT_RATE,
            "--pitch=" + DEFAULT_PITCH,
            "--text",
            text,
            "--write-media",
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or str(e))[:300]
            raise RuntimeError("edge-tts 合成失败: " + err) from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("edge-tts 合成超时") from e
        data = out.read_bytes()
        if not data:
            raise RuntimeError("edge-tts 产出空文件")
        return data


def speak(text: str, user_id=None, group_id=None, voice: str = DEFAULT_VOICE, dry_run: bool = False):
    """合成并发送；dry_run 只合成不发，返回 (b64, None)。"""
    audio = synthesize_mp3(text, voice=voice)
    b64 = base64.b64encode(audio).decode("ascii")
    if dry_run:
        return b64, None
    res = napcat_send_record(b64, user_id=user_id, group_id=group_id)
    return b64, res


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="大肥鱼 TTS 冒烟/发语音")
    p.add_argument("--text", required=True)
    p.add_argument("--user-id", default="")
    p.add_argument("--group-id", default="")
    p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    uid = args.user_id.strip() or None
    gid = args.group_id.strip() or None
    if not args.dry_run and bool(uid) == bool(gid):
        print("需要且只能指定 --user-id 或 --group-id", file=sys.stderr)
        return 2
    b64, res = speak(args.text, user_id=uid, group_id=gid, voice=args.voice, dry_run=args.dry_run)
    print("bytes_b64_len=", len(b64))
    if res is not None:
        print("napcat=", res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
