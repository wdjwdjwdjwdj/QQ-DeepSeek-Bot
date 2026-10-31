# -*- coding: utf-8 -*-
"""把已有 TokenHub 聊天模型 tx-hy3 挂进 MaiBot 对话池中后段。

功能：幂等改写 model_config.toml 的 replyer / utils / fast_model（以及存在的 fast）
model_list，把别名 tx-hy3 插在付费尾（deepseek-flash-reply / deepseek-flash /
deepseek-flash-planner）和名字里带 vlm 的模型之前。不改 planner，不改 embedding /
image_embedding，不新增 provider，不写密钥。

调用：服务器上手工执行（非 cron）::

    python3 deploy/add_tokenhub_chat.py

model_config.toml 被 gitignore。本机没有该文件时打印 SKIP 并以 0 退出。
生产路径同时认仓库内 my-maibot/config/model_config.toml 与
/root/maibot/my-maibot/config/model_config.toml。

网络边界：不出网、不发 QQ。
"""
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import botcommon
import token_guard

POOL_TASKS = ("replyer", "utils", "fast_model", "fast")
REQUIRED_TASKS = ("replyer", "utils")
PROTECTED_TASKS = ("planner", "embedding", "image_embedding")
PAID_TAIL = {"deepseek-flash-reply", "deepseek-flash", "deepseek-flash-planner"}


def _is_pool_tail(name):
    """付费兜底和视觉模型留在 tx-hy3 后面。"""
    return name in PAID_TAIL or "vlm" in name.lower()


def insert_tokenhub_chat(model_list, alias=None):
    """把 TokenHub 聊天别名放到免费段末尾、付费/vlm 尾之前。已在则只归一位置。"""
    alias = alias or botcommon.TOKENHUB_POOL_ALIAS
    rest = [m for m in model_list if m != alias]
    split = len(rest)
    for i, name in enumerate(rest):
        if _is_pool_tail(name):
            split = i
            break
    return rest[:split] + [alias] + rest[split:]


def _config_candidates():
    return (
        ROOT / "my-maibot" / "config" / "model_config.toml",
        Path("/root/maibot/my-maibot/config/model_config.toml"),
    )


def find_config():
    for path in _config_candidates():
        if path.is_file():
            return path
    return None


def _require_tx_hy3(cfg):
    found = None
    for model in cfg.get("models") or []:
        if model.get("name") == botcommon.TOKENHUB_POOL_ALIAS:
            found = model
            break
    if found is None:
        names = [str(m.get("name")) for m in (cfg.get("models") or [])]
        raise RuntimeError(
            "model_config 里没有名为 %s 的模型，拒绝另挑。现有 name=%s"
            % (botcommon.TOKENHUB_POOL_ALIAS, ",".join(names))
        )
    provider = found.get("api_provider")
    if provider != "TencentMaaS":
        raise RuntimeError(
            "%s 的 api_provider=%r，期望 TencentMaaS"
            % (botcommon.TOKENHUB_POOL_ALIAS, provider)
        )
    return found


def _assert_protected(before, after):
    old = token_guard.read_task_lists(before)
    new = token_guard.read_task_lists(after)
    for task in PROTECTED_TASKS:
        if old.get(task) != new.get(task):
            raise RuntimeError("拒绝：%s 被改动 %s -> %s" % (task, old.get(task), new.get(task)))
    planner = new.get("planner") or []
    if planner and planner[0] != (old.get("planner") or [None])[0]:
        raise RuntimeError("拒绝：planner 首位被改动")


def patch_model_config_text(text):
    """返回改写后的 toml 文本。无聊天池变化时原样返回。"""
    cfg = tomllib.loads(text)
    model = _require_tx_hy3(cfg)
    ident = str(model.get("model_identifier") or "").strip()
    print(
        "%s model_identifier=%s"
        % (botcommon.TOKENHUB_POOL_ALIAS, ident or "(空)")
    )
    if ident and ident != botcommon.TOKENHUB_CHAT_MODEL:
        print(
            "注意: toml identifier 与级联默认 %s 不同；llm_free_chat 会改用 toml"
            % botcommon.TOKENHUB_CHAT_MODEL
        )
    current = token_guard.read_task_lists(cfg)
    updates = {}
    for task in POOL_TASKS:
        if task not in current:
            if task in REQUIRED_TASKS:
                raise RuntimeError("找不到 model_task_config.%s" % task)
            print("跳过不存在的池:", task)
            continue
        updated = insert_tokenhub_chat(list(current[task]))
        if updated != list(current[task]):
            print("  %s: %s -> %s" % (task, current[task], updated))
            updates[task] = updated
    if not updates:
        return text
    new_text = token_guard.set_task_model_lists(text, updates)
    parsed = tomllib.loads(new_text)
    got = token_guard.read_task_lists(parsed)
    for task, expected in updates.items():
        if got.get(task) != expected:
            raise RuntimeError("写回校验失败 %s: %s != %s" % (task, got.get(task), expected))
    _assert_protected(cfg, parsed)
    return new_text


def apply_file(path, dry_run=False):
    raw = Path(path).read_text(encoding="utf-8")
    new = patch_model_config_text(raw)
    if new == raw:
        print("model_list 无变化", path)
        return False
    if dry_run:
        print("dry-run: 不写盘", path)
        return True
    tmp = Path(path).with_suffix(Path(path).suffix + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    print("已原子写回", path)
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    path = find_config()
    if path is None:
        print("SKIP: 未找到 model_config.toml（gitignore，不在本仓库）。到服务器 /root/maibot 再执行。")
        return 0
    try:
        apply_file(path, dry_run=dry_run)
    except Exception as e:
        print("FAIL:", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
