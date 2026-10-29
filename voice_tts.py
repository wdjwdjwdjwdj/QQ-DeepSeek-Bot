# -*- coding: utf-8 -*-
"""voice_tts.py — edge-tts 合成 + NapCat 发语音

功能：把文本合成中文女声 mp3，再经本机 NapCat HTTP 发成 QQ 语音。
调用方：VoiceSpeak 插件（聊天里 LLM 工具）；也可 CLI 冒烟。
依赖：/root/maibot/.venv-tts（edge-tts），标准库 subprocess；网络走 botcommon。
用法：
  python3 voice_tts.py --text "你好" --user-id 2968874474
  python3 voice_tts.py --text "群测" --group-id 123456 --dry-run
"""
from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import tempfile
from pathlib import Path

from botcommon import napcat_send_record

BASE = Path(__file__).resolve().parent
EDGE_TTS = BASE / ".venv-tts" / "bin" / "edge-tts"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


def synthesize_mp3(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    """调用 edge-tts CLI 合成 mp3 字节；失败抛 RuntimeError。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("合成文本为空")
    if len(text) > 500:
        raise ValueError("合成文本过长（上限 500 字）")
    if not EDGE_TTS.exists():
        raise RuntimeError("未找到 edge-tts：请先建 /root/maibot/.venv-tts 并 pip install edge-tts")
    with tempfile.TemporaryDirectory(prefix="maibot-tts-") as td:
        out = Path(td) / "out.mp3"
        cmd = [
            str(EDGE_TTS),
            "--voice",
            voice,
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
