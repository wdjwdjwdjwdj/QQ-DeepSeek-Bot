# -*- coding: utf-8 -*-
"""SchedulerManager — 定时消息管家插件

向 MaiBot 的 LLM 注册三个工具（schedule_create / schedule_list / schedule_delete），
让大肥鱼在聊天里听懂"每天早上8点给我发早安"这类请求并自己创建定时任务。
任务写入项目根目录 schedule_config.json（临时文件 + os.replace 原子替换），
由外部计划任务 scheduled_sender.py 每分钟读取并发送（走 NapCat :3002）。

权限：仅限哥哥（QQ 2968874474）。user_id / group_id 由宿主从真实消息上下文注入，
不在工具参数表中声明，因此不能被对话内容伪造。
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from maibot_sdk import MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

OWNER_QQ = "2968874474"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

BASE = Path(__file__).resolve().parents[3]  # plugins/SchedulerManager -> my-maibot -> QQ-DeepSeek-Bot
CONFIG_FILE = BASE / "schedule_config.json"

WEEKDAY_NAMES = "日一二三四五六"


def _load_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps({"tasks": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _save_config(cfg: Dict[str, Any]) -> None:
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def _parse_days(raw: str) -> Optional[List[int]]:
    raw = (raw or "").strip()
    if not raw or raw == "每天":
        return None
    days: List[int] = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not (0 <= int(part) <= 6):
            raise ValueError("days 不合法: " + part + "（0=周日, 1-6=周一到周六）")
        day = int(part)
        if day not in days:
            days.append(day)
    return sorted(days) if days else None


def _parse_target(raw: str, group_id: str, user_id: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    if not raw or raw in ("current", "当前聊天"):
        if group_id:
            return {"type": "group", "id": int(group_id)}
        if user_id:
            return {"type": "private", "id": int(user_id)}
        raise ValueError("无法确定当前聊天对象，请显式传 target")
    match = re.match(r"^(private|私聊|group|群聊)[:：](\d+)$", raw)
    if not match:
        raise ValueError("target 格式应为 private:QQ号 或 group:群号，留空表示当前聊天")
    target_type = "group" if match.group(1) in ("group", "群聊") else "private"
    return {"type": target_type, "id": int(match.group(2))}


STATE_FILE = BASE / "scheduled_sender_state.json"


def _sent_once_keys() -> set:
    """读取 sender 的 state，返回已完成的一次性任务键集合。"""
    if not STATE_FILE.exists():
        return set()
    try:
        return {k for k in json.loads(STATE_FILE.read_text(encoding="utf-8")).get("sent", []) if k.startswith("once ")}
    except Exception:
        return set()


def _fmt_task(task: Dict[str, Any]) -> str:
    target = task.get("target", {})
    if target.get("type") == "group":
        where = "群 " + str(target.get("id"))
    else:
        where = "私聊 " + str(target.get("id"))
    state = "启用" if task.get("enabled", True) else "停用"
    once = str(task.get("once") or "").strip()
    if once:
        done = ("once " + once + " " + str(task.get("name"))) in _sent_once_keys()
        return "[" + str(task.get("name")) + "] 一次性 " + once + " → " + where + "（" + ("已发送" if done else state) + "）"
    when = "/".join(task.get("times", []))
    days = task.get("days")
    day_str = "每天" if not days else "周" + "、周".join(WEEKDAY_NAMES[d] for d in days)
    return "[" + str(task.get("name")) + "] " + day_str + " " + when + " → " + where + "（" + state + "）"


class SchedulerManagerPlugin(MaiBotPlugin):
    """定时消息管家：LLM 工具版。"""

    def _owner_error(self, kwargs: Dict[str, Any]) -> Optional[str]:
        user_id = str(kwargs.get("user_id") or "").strip()
        if user_id != OWNER_QQ:
            return "权限不足：定时任务只有哥哥能管理（当前请求者 " + (user_id or "未知") + "），请委婉告诉对方。"
        return None

    @Tool(
        "schedule_create",
        description=(
            "创建定时消息任务。当哥哥（主人）要求定时/每天固定时间/指定某天某刻发消息、提醒、通知时调用。"
            "两类用法：①周期任务：time=HH:MM（每天或按 days）；②一次性任务：once='YYYY-MM-DD HH:MM'（如宣讲会通知、"
            "某天早上的提醒，此时不要传 time/days）。"
            "message 用你自己的口吻写；要在群里@全体成员，消息开头加 [CQ:at,qq=all]（需要群管理员权限，哥哥已授权）。"
            "仅限哥哥本人使用，别人要求时不要调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="task_name",
                param_type=ToolParamType.STRING,
                description="简短中文任务名，如：早安问候、宣讲会通知",
                required=True,
            ),
            ToolParameterInfo(
                name="message",
                param_type=ToolParamType.STRING,
                description="要定时发送的消息内容（用你的口吻写；群里@全体则开头加 [CQ:at,qq=all]）",
                required=True,
            ),
            ToolParameterInfo(
                name="once",
                param_type=ToolParamType.STRING,
                description="一次性任务的触发时间，格式 YYYY-MM-DD HH:MM，如 2026-10-26 09:00。与 time 二选一",
                required=False,
            ),
            ToolParameterInfo(
                name="time",
                param_type=ToolParamType.STRING,
                description="周期任务的发送时间，24小时制 HH:MM；多个时间用英文逗号分隔。与 once 二选一",
                required=True,
            ),
            ToolParameterInfo(
                name="more_messages",
                param_type=ToolParamType.ARRAY,
                description="可选的消息变体（2-3 条，风格不同），发送时与 message 一起随机选一条",
                required=False,
                items_schema={"type": "string"},
            ),
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="发给谁：留空=当前聊天；private:QQ号=私聊某人；group:群号=发到某群",
                required=False,
            ),
            ToolParameterInfo(
                name="days",
                param_type=ToolParamType.STRING,
                description="哪些天发：留空=每天；工作日=1,2,3,4,5；周末=0,6（0=周日, 1-6=周一到周六）",
                required=False,
            ),
        ],
    )
    async def schedule_create(
        self,
        task_name: str = "",
        time: str = "",
        message: str = "",
        once: str = "",
        more_messages: Optional[List[str]] = None,
        target: str = "",
        days: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        err = self._owner_error(kwargs)
        if err:
            return {"success": False, "error": err}

        name = str(task_name or "").strip()
        if not name:
            return {"success": False, "error": "task_name 不能为空"}

        messages: List[str] = [str(message or "").strip()]
        for extra in more_messages or []:
            extra = str(extra).strip()
            if extra and extra not in messages:
                messages.append(extra)
        if not messages[0]:
            return {"success": False, "error": "message 不能为空"}

        try:
            target_dict = _parse_target(
                target,
                str(kwargs.get("group_id") or "").strip(),
                str(kwargs.get("user_id") or "").strip(),
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        once_str = str(once or "").strip()
        if once_str:
            try:
                datetime.strptime(once_str, "%Y-%m-%d %H:%M")
            except ValueError:
                return {"success": False, "error": "once 格式应为 YYYY-MM-DD HH:MM，如 2026-10-26 09:00"}
            if str(time or "").strip():
                return {"success": False, "error": "once 与 time 只能二选一"}
            days_list = None
            times: List[str] = []
        else:
            times = []
            for part in str(time or "").replace("，", ",").split(","):
                part = part.strip()
                if not part:
                    continue
                if not TIME_RE.match(part):
                    return {"success": False, "error": "时间格式不对: " + part + "（应为 24 小时制 HH:MM）"}
                if part not in times:
                    times.append(part)
            if not times:
                return {"success": False, "error": "必须提供 time（周期任务）或 once（一次性任务）之一"}
            try:
                days_list = _parse_days(days)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}

        cfg = _load_config()
        if any(t.get("name") == name for t in cfg.get("tasks", [])):
            return {
                "success": False,
                "error": "任务名 [" + name + "] 已存在，换个名字或先用 schedule_delete 删除",
            }

        task = {
            "name": name,
            "enabled": True,
            "target": target_dict,
            "times": times,
            "days": days_list,
            "messages": messages,
        }
        if once_str:
            task["once"] = once_str
        cfg.setdefault("tasks", []).append(task)
        _save_config(cfg)
        self.ctx.logger.info("schedule_create: " + name + " by " + str(kwargs.get("user_id")))
        return {
            "success": True,
            "content": "定时任务创建成功：" + _fmt_task(task) + "，消息池 " + str(len(messages)) + " 条，下一分钟内生效。",
        }

    @Tool(
        "schedule_list",
        description="查看当前全部定时消息任务。当哥哥问'你现在有哪些定时任务/闹钟'时调用。仅限哥哥本人使用。",
    )
    async def schedule_list(self, **kwargs: Any) -> Dict[str, Any]:
        err = self._owner_error(kwargs)
        if err:
            return {"success": False, "error": err}

        tasks = _load_config().get("tasks", [])
        if not tasks:
            return {"success": True, "content": "目前没有任何定时任务。"}
        lines = [_fmt_task(t) for t in tasks]
        return {"success": True, "content": "当前定时任务 " + str(len(lines)) + " 条：\n" + "\n".join(lines)}

    @Tool(
        "schedule_delete",
        description=(
            "按任务名删除定时消息任务。当哥哥要求取消/删掉某个定时任务时调用。"
            "可先用 schedule_list 查任务名。仅限哥哥本人使用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="task_name",
                param_type=ToolParamType.STRING,
                description="要删除的任务名（必须与现有任务名一致）",
                required=True,
            ),
        ],
    )
    async def schedule_delete(self, task_name: str = "", **kwargs: Any) -> Dict[str, Any]:
        err = self._owner_error(kwargs)
        if err:
            return {"success": False, "error": err}

        name = str(task_name or "").strip()
        cfg = _load_config()
        tasks = cfg.get("tasks", [])
        remaining = [t for t in tasks if t.get("name") != name]
        if len(remaining) == len(tasks):
            return {"success": False, "error": "没找到任务 [" + name + "]，可先用 schedule_list 查看任务名"}
        cfg["tasks"] = remaining
        _save_config(cfg)
        self.ctx.logger.info("schedule_delete: " + name + " by " + str(kwargs.get("user_id")))
        return {"success": True, "content": "定时任务 [" + name + "] 已删除。"}

    async def on_load(self) -> None:
        self.ctx.logger.info("SchedulerManager 已加载，配置文件: " + str(CONFIG_FILE))

    async def on_unload(self) -> None:
        pass

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        pass


def create_plugin() -> SchedulerManagerPlugin:
    return SchedulerManagerPlugin()
