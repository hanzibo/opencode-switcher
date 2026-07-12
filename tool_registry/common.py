"""Common/shared tools — small cross-domain tools that don't belong to a specific domain."""

import datetime
import json
import os
from typing import Final, Tuple

_IGNORE_DIRS: Final = {"node_modules", "venv", ".venv", "env", "__pycache__",
                       "build", "dist", "target", "cache", ".cache"}


def _get_ignore_dirs() -> set:
    config_path = os.path.expanduser("~/.config/opencode-switcher/config.json")
    ignore_dirs = set(_IGNORE_DIRS)
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
            custom_ignores = config.get("search_ignore_dirs", [])
            if isinstance(custom_ignores, list):
                for d in custom_ignores:
                    if isinstance(d, str) and d.strip():
                        ignore_dirs.add(d.strip())
        except Exception:
            pass
    return ignore_dirs


_WEEKDAYS_CN: Final[Tuple[str, ...]] = (
    "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"
)


def execute_get_current_time(timezone: str = "") -> str:
    """Get current date, time, weekday and timezone info.

    Args:
        timezone: IANA timezone name (e.g. "Asia/Shanghai", "UTC").
                  Empty string means system local time.

    Returns:
        Formatted string with current time info.
    """
    if timezone:
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(timezone)
            now = datetime.datetime.now(tz)
        except (ImportError, ModuleNotFoundError):
            return f"错误：当前 Python 版本不支持 zoneinfo，无法使用时区参数「{timezone}」。请留空 timezone 使用本地时间。"
        except (KeyError, TypeError, OSError):
            return f"错误：无效的时区名称「{timezone}」"
    else:
        now = datetime.datetime.now().astimezone()

    tz_name = now.tzname() or "?"
    weekday = _WEEKDAYS_CN[now.weekday()]
    ts = int(now.timestamp())

    offset = now.utcoffset()
    if offset is not None:
        total_minutes = int(offset.total_seconds() // 60)
        offset_hours = total_minutes // 60
        offset_minutes = abs(total_minutes) % 60
        offset_str = f"UTC{offset_hours:+d}" + (f":{offset_minutes:02d}" if offset_minutes else "")
    else:
        offset_str = "?"

    return (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"ISO 8601：{now.isoformat()}\n"
        f"星期：{weekday}\n"
        f"时区：{tz_name} ({offset_str})\n"
        f"时间戳：{ts}"
    )


def execute_ask_user_question(question: str) -> str:
    """Ask the user a question and return their response.

    Note: This is a fallback — the actual blocking user interaction
    is handled by clipboard_panel.py which intercepts this tool in
    ai_tool_loop.py before calling execute_tool_call().

    If this function is reached (interception failed), it returns an error
    to prevent the agent from receiving a fake success response.
    """
    return "错误：ask_user_question 未被拦截，用户提问已丢失。请使用其他方式获取所需信息。"


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前的日期、时间、星期和时区信息。可指定 IANA 时区（如 Asia/Shanghai、UTC）或使用本地时间。不适用于计时、倒计时、闹钟或时间运算。仅返回当前时刻的快照。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA 时区名称，如 Asia/Shanghai、UTC、America/New_York 等。留空则使用系统本地时间。",
                        "default": ""
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "description": "向用户提问并等待回答。当需要用户确认、选择或提供额外信息时使用。注意：此工具会阻塞等待用户响应。仅在需要用户输入额外信息时调用。不适用于自行搜索信息或做出假设。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "需要向用户提出的问题"
                    }
                },
                "required": ["question"]
            }
        }
    },
]
