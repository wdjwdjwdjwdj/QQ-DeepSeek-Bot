# -*- coding: utf-8 -*-
"""把不依赖 tool_call 的旁路任务钉进免费 sequential 池，并让 Z.AI 给智谱让位。

功能：幂等改写 model_config.toml（gitignore，不进仓库）。
- emoji / learner：selection_strategy=sequential，model_list 换成配置里真实存在的
  免费顺序（智谱 → Z.AI → OpenRouter → 商汤/TokenHub 尾）。去掉字面量 balance
  和 deepseek-flash-planner / 付费回复名。空列表会让表情替换落到 planner，所以不允许写成空。
- replyer / utils / fast_model / fast：只把 zai-flash 挪到本池最后一个智谱模型后面，
  不增删名字，不动 planner 首位，不动 memory / voice / vlm / embedding。
- ZAI 这个 api_provider 的 max_retry / retry_interval 收到 1，429 不要在同一模型上空转。

核对依据（MaiBot 源码，不是猜任务名）：
- emoji_manager.replace_an_emoji_by_llm：model_list 为空则 task_name="planner"，request_type="emoji"
- send_emoji 子代理默认不传 tool_definitions
- learners/* 的 generate_response 不传 tools；空 learner 会继用 utils

调用：非 cron，服务器手工执行::

    python3 deploy/apply_free_pool_routing.py
    python3 deploy/apply_free_pool_routing.py --dry-run

本机没有 model_config.toml 时打印 SKIP 并以 0 退出。
生产路径同时认仓库内 my-maibot/config/model_config.toml 与
/root/maibot/my-maibot/config/model_config.toml。

网络边界：不出网、不发 QQ、不写密钥。
"""
import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import botcommon
import token_guard

PROTECTED_TASKS = (
    "planner",
    "memory",
    "voice",
    "vlm",
    "embedding",
    "image_embedding",
)


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


def _section_exists(text, task):
    """段头允许行尾注释；生产 toml 常见 [model_task_config.x] # 说明。"""
    return (
        re.search(
            r"(?m)^\[model_task_config\.%s\]\s*(?:#.*)?$" % re.escape(task),
            text,
        )
        is not None
    )


def ensure_aux_section(text, task, model_list):
    """没有该任务段时追加。已有则原样返回。"""
    if _section_exists(text, task):
        return text
    block = (
        "\n[model_task_config.%s]\nmodel_list = %s\nselection_strategy = \"sequential\"\n"
        % (task, token_guard._fmt_list(model_list))
    )
    return text.rstrip() + "\n" + block


def set_selection_strategy(toml_text, task, strategy):
    """只改指定任务段里的 selection_strategy。没有这一行就加在段尾。"""
    match = re.search(
        r"(\[model_task_config\.%s\])(.*?)(?=\n\[|\Z)" % re.escape(task),
        toml_text,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError("找不到 model_task_config.%s" % task)
    body = match.group(2)
    strat = re.search(r"(selection_strategy\s*=\s*)([\"']).*?\2", body)
    if strat:
        new_body = body[: strat.start()] + '%s"%s"' % (strat.group(1), strategy) + body[strat.end() :]
    else:
        new_body = body.rstrip() + '\nselection_strategy = "%s"\n' % strategy
    return toml_text[: match.start(2)] + new_body + toml_text[match.end(2) :]


def tighten_zai_provider(text):
    """只收紧 name=ZAI 的 api_provider 重试。其他 provider 原样。没有 ZAI 则不改。"""
    parts = re.split(r"(?=\[\[api_providers\]\])", text)
    found = False
    out = []
    max_retry = botcommon.ZAI_PROVIDER_MAX_RETRY
    interval = botcommon.ZAI_PROVIDER_RETRY_INTERVAL
    for part in parts:
        if not part.startswith("[[api_providers]]"):
            out.append(part)
            continue
        if re.search(r"(?m)^name\s*=\s*\"ZAI\"\s*$", part) is None:
            out.append(part)
            continue
        found = True
        new = part

        def _cap(pattern, limit, source):
            matched = re.search(pattern, source)
            if not matched:
                return source, False
            try:
                current = int(matched.group(2))
            except ValueError:
                return source, False
            if current <= limit:
                return source, False
            replaced = source[: matched.start()] + matched.group(1) + str(limit) + source[matched.end() :]
            return replaced, True

        new, _ = _cap(r"(max_retry\s*=\s*)(\d+)", max_retry, new)
        new, _ = _cap(r"(retry_interval\s*=\s*)(\d+)", interval, new)
        if re.search(r"max_retry\s*=", new) is None:
            new = re.sub(
                r"(?m)^(name\s*=\s*\"ZAI\"\s*)$",
                "\\1\nmax_retry = %s\nretry_interval = %s" % (max_retry, interval),
                new,
                count=1,
            )
        elif re.search(r"retry_interval\s*=", new) is None:
            new = re.sub(
                r"(?m)^(max_retry\s*=\s*\d+\s*)$",
                "\\1\nretry_interval = %s" % interval,
                new,
                count=1,
            )
        out.append(new)
    if not found:
        print("没有名为 ZAI 的 api_provider，跳过重试收紧")
    return "".join(out)


def _task_view(cfg):
    mtc = cfg.get("model_task_config") or {}
    view = {}
    for task, body in mtc.items():
        if isinstance(body, dict) and "model_list" in body:
            view[task] = {
                "model_list": list(body.get("model_list") or []),
                "selection_strategy": body.get("selection_strategy"),
            }
    return view


def _assert_safe(before, after, aux):
    old = _task_view(before)
    new = _task_view(after)
    for task in PROTECTED_TASKS:
        if old.get(task) != new.get(task):
            raise RuntimeError("拒绝：%s 被改动 %s -> %s" % (task, old.get(task), new.get(task)))
    old_planner = (old.get("planner") or {}).get("model_list") or []
    new_planner = (new.get("planner") or {}).get("model_list") or []
    if old_planner and new_planner and old_planner[0] != new_planner[0]:
        raise RuntimeError("拒绝：planner 首位被改动")
    forbidden = set(botcommon.AUX_FORBIDDEN_MODELS)
    for task in botcommon.NON_TOOL_AUX_TASKS:
        got = (new.get(task) or {}).get("model_list") or []
        if got != aux:
            raise RuntimeError("%s 模型列表不是免费顺序池: %s" % (task, got))
        if forbidden.intersection(got):
            raise RuntimeError("%s 仍含禁止模型: %s" % (task, sorted(forbidden.intersection(got))))
        if (new.get(task) or {}).get("selection_strategy") != "sequential":
            raise RuntimeError("%s 策略不是 sequential" % task)
    for task in botcommon.CHAT_POOLS_YIELD_ZAI:
        if task not in old:
            continue
        if sorted(old[task]["model_list"]) != sorted(new[task]["model_list"]):
            raise RuntimeError("%s 增删了模型: %s -> %s" % (task, old[task]["model_list"], new[task]["model_list"]))
        if old[task]["selection_strategy"] != new[task]["selection_strategy"]:
            raise RuntimeError("%s 的 selection_strategy 被改动" % task)
    old_keys = [p.get("api_key") for p in before.get("api_providers") or []]
    new_keys = [p.get("api_key") for p in after.get("api_providers") or []]
    if old_keys != new_keys:
        raise RuntimeError("拒绝：api_key 被改动")
    if before.get("models") != after.get("models"):
        raise RuntimeError("拒绝：models 条目被改动")


def apply_routing_text(text):
    """返回改写后的 toml。无变化时原样返回。"""
    cfg = tomllib.loads(text)
    names = [str(model.get("name")) for model in (cfg.get("models") or []) if model.get("name")]
    aux = botcommon.free_aux_model_list(names)
    missing = [name for name in botcommon.FREE_AUX_MODEL_ORDER if name not in set(names)]
    if missing:
        print("旁路池跳过配置里没有的名字:", ", ".join(missing))
    if not aux:
        raise RuntimeError(
            "没有可用的免费旁路模型，拒绝把 emoji/learner 写成空列表（空列表会落到 planner）"
        )
    mtc = cfg.get("model_task_config") or {}
    new_text = text
    updates = {}
    strategy_tasks = []
    for task in botcommon.NON_TOOL_AUX_TASKS:
        if not _section_exists(new_text, task):
            new_text = ensure_aux_section(new_text, task, aux)
            print("追加任务段:", task)
            continue
        current = list((mtc.get(task) or {}).get("model_list") or [])
        strategy = str((mtc.get(task) or {}).get("selection_strategy") or "")
        if current != aux:
            print("  %s: %s -> %s" % (task, current, aux))
            updates[task] = aux
        if strategy != "sequential":
            strategy_tasks.append(task)
    for task in botcommon.CHAT_POOLS_YIELD_ZAI:
        body = mtc.get(task)
        if not isinstance(body, dict) or "model_list" not in body:
            continue
        current = list(body.get("model_list") or [])
        moved = botcommon.move_zai_after_zhipu(current)
        if moved != current:
            print("  %s Z.AI 让位: %s -> %s" % (task, current, moved))
            updates[task] = moved
    if updates:
        new_text = token_guard.set_task_model_lists(new_text, updates)
    for task in strategy_tasks:
        new_text = set_selection_strategy(new_text, task, "sequential")
        print("  %s strategy -> sequential" % task)
    tightened = tighten_zai_provider(new_text)
    parsed = tomllib.loads(tightened)
    _assert_safe(cfg, parsed, aux)
    return tightened


def apply_file(path, dry_run=False):
    raw = Path(path).read_text(encoding="utf-8")
    new = apply_routing_text(raw)
    if new == raw:
        print("路由无变化", path)
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
