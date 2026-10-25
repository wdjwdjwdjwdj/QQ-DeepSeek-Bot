# -*- coding: utf-8 -*-
"""verify_scheduler.py — SchedulerManager 插件逻辑单测

不经过 MaiBot Runner，直接实例化插件并模拟宿主注入的 kwargs 调用三个工具。
覆盖：权限拦截（非哥拒绝）、时间/天数/target 校验、群聊上下文默认值、
建/查/删闭环。测试前快照 schedule_config.json，结束后原样恢复（零污染）。

用法：py verify_scheduler.py
"""
import asyncio
import importlib.util
import json
import logging
import os
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent
PLUGIN = BASE / "my-maibot" / "plugins" / "SchedulerManager" / "plugin.py"
CONFIG = BASE / "schedule_config.json"
BACKUP = BASE / "schedule_config.json.bak-verify"

OWNER = "2968874474"

PASS = 0
FAIL = 0
_EXIT_CODE = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class DummyCtx:
    logger = logging.getLogger("verify")


def _ensure_maibot_sdk():
    """系统 Python 没有 maibot_sdk（它随 maibot.exe 的 uv 环境分发），
    从 uv cache 的 archive 目录里自动发现并挂到 sys.path。"""
    try:
        import maibot_sdk  # noqa: F401
        return
    except ImportError:
        pass
    import glob
    import sys
    hits = glob.glob(os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "uv", "cache", "archive-v0", "*", "maibot_sdk", "__init__.py"))
    if hits:
        sys.path.insert(0, os.path.dirname(os.path.dirname(hits[0])))


def load_plugin():
    _ensure_maibot_sdk()
    spec = importlib.util.spec_from_file_location("sched_plugin_verify", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.create_plugin()
    plugin._set_context(DummyCtx())
    return plugin


async def main():
    if BACKUP.exists():
        raise SystemExit("发现残留备份 " + BACKUP.name + "（上次异常退出），先手动恢复再跑，避免覆盖真实配置")
    if CONFIG.exists():
        shutil.copyfile(CONFIG, BACKUP)
    CONFIG.write_text(json.dumps({"tasks": []}, ensure_ascii=False), encoding="utf-8")

    try:
        await run_tests()
    finally:
        if BACKUP.exists():
            shutil.copyfile(BACKUP, CONFIG)
            BACKUP.unlink()
            print("schedule_config.json 已恢复原样")
        else:
            print("警告：无备份可恢复（本次为空配置起跑）")


async def run_tests():

    plugin = load_plugin()

    print("[1] 权限拦截：非哥建任务必须被拒")
    r = await plugin.schedule_create(task_name="越权", time="08:00", message="x", user_id="12345", platform="qq")
    check("非哥创建被拒", r.get("success") is False and "权限" in str(r.get("error", "")), str(r))

    r = await plugin.schedule_delete(task_name="anything", user_id="12345")
    check("非哥删除被拒", r.get("success") is False)

    r = await plugin.schedule_list(user_id="12345")
    check("非哥查看被拒", r.get("success") is False)

    print("[2] 哥私聊创建：target 默认=当前聊天（私聊哥）")
    r = await plugin.schedule_create(
        task_name="单测早安", time="08:05，21:05", message="哥早~",
        more_messages=["早啊哥", "起床啦"], user_id=OWNER, platform="qq",
    )
    check("创建成功", r.get("success") is True, str(r))
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    t = next((x for x in cfg["tasks"] if x["name"] == "单测早安"), None)
    check("target=私聊哥", t and t["target"] == {"type": "private", "id": int(OWNER)}, str(t))
    check("两个时间点", t and t["times"] == ["08:05", "21:05"], str(t and t["times"]))
    check("消息池3条", t and len(t["messages"]) == 3, str(t and t["messages"]))
    check("days=None(每天)", t and t["days"] is None)

    print("[3] 哥在群里创建：target 默认=当前群")
    r = await plugin.schedule_create(
        task_name="单测群晚安", time="23:10", message="晚安各位~",
        user_id=OWNER, group_id="999888", platform="qq",
    )
    t = next((x for x in json.loads(CONFIG.read_text(encoding="utf-8"))["tasks"] if x["name"] == "单测群晚安"), None)
    check("target=当前群", t and t["target"] == {"type": "group", "id": 999888}, str(t))

    print("[4] 非法输入必须被拒")
    r = await plugin.schedule_create(task_name="坏时间", time="8点半", message="x", user_id=OWNER)
    check("中文时间被拒", r.get("success") is False, str(r))
    r = await plugin.schedule_create(task_name="坏时间", time="25:00", message="x", user_id=OWNER)
    check("25:00 被拒", r.get("success") is False)
    r = await plugin.schedule_create(task_name="坏target", time="08:00", message="x", target="随便发给张三", user_id=OWNER)
    check("非法 target 被拒", r.get("success") is False)
    r = await plugin.schedule_create(task_name="坏days", time="08:00", message="x", days="8", user_id=OWNER)
    check("days=8 被拒", r.get("success") is False)
    r = await plugin.schedule_create(task_name="单测早安", time="08:00", message="x", user_id=OWNER)
    check("重名被拒", r.get("success") is False)

    print("[5] 工作日 days 解析")
    r = await plugin.schedule_create(task_name="单测工作日", time="12:00", message="午安", days="1,2,3,4,5", user_id=OWNER)
    t = next((x for x in json.loads(CONFIG.read_text(encoding="utf-8"))["tasks"] if x["name"] == "单测工作日"), None)
    check("days=[1..5]", t and t["days"] == [1, 2, 3, 4, 5], str(t and t["days"]))

    print("[5.5] 一次性任务（once）")
    r = await plugin.schedule_create(
        task_name="单测宣讲", once="2026-10-26 09:00", message="[CQ:at,qq=all] 今晚宣讲会",
        user_id=OWNER, group_id="1125309087",
    )
    check("once 创建成功", r.get("success") is True, str(r))
    t = next((x for x in json.loads(CONFIG.read_text(encoding="utf-8"))["tasks"] if x["name"] == "单测宣讲"), None)
    check("once 字段正确", t and t.get("once") == "2026-10-26 09:00", str(t))
    check("once 无 times", t and t.get("times") == [])
    r = await plugin.schedule_create(task_name="冲突", once="2026-10-26 09:00", time="08:00", message="x", user_id=OWNER)
    check("once+time 同时给被拒", r.get("success") is False)
    r = await plugin.schedule_create(task_name="坏once", once="明天早上九点", message="x", user_id=OWNER)
    check("once 中文格式被拒", r.get("success") is False)

    print("[6] 查询与删除闭环")
    r = await plugin.schedule_list(user_id=OWNER)
    check("列表含4个单测任务", r.get("success") is True and r.get("content", "").count("单测") == 4, str(r))
    r = await plugin.schedule_delete(task_name="单测早安", user_id=OWNER)
    check("删除成功", r.get("success") is True, str(r))
    r = await plugin.schedule_delete(task_name="不存在", user_id=OWNER)
    check("删不存在报错", r.get("success") is False)

    print(f"\n结果: {PASS} passed, {FAIL} failed")
    if FAIL:
        global _EXIT_CODE
        _EXIT_CODE = 1


if __name__ == "__main__":
    asyncio.run(main())
    raise SystemExit(_EXIT_CODE)
