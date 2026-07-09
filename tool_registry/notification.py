"""Desktop notification tool — send system notifications via notify-send."""

import subprocess


def execute_send_notification(
    summary: str,
    body: str = "",
    urgency: str = "normal",
    expire_time: int = 5000,
    icon: str = "",
) -> str:
    """Send a desktop notification via notify-send.

    Args:
        summary: Notification title/summary.
        body: Optional notification body text.
        urgency: Urgency level — "low", "normal", or "critical".
        expire_time: Display duration in milliseconds (default 5000).
        icon: Icon name or path (freedesktop icon name like "dialog-information").

    Returns:
        Structured result with status, summary, and details.
    """
    try:
        cmd = ["notify-send", "-a", "OpenCode Switcher"]

        if urgency in ("low", "normal", "critical"):
            cmd.extend(["-u", urgency])

        if expire_time > 0:
            cmd.extend(["-t", str(expire_time)])

        if icon:
            cmd.extend(["-i", icon])

        cmd.append(summary)
        if body:
            cmd.append(body)

        result = subprocess.run(
            cmd,
            timeout=10,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            err = result.stderr.strip()
            error_msg = err if err else f"返回码 {result.returncode}"
            return f"❌ 通知发送失败\n   原因: {error_msg}\n   标题: {summary}"

        return f"✅ 通知已发送\n   标题: {summary}\n   正文: {body or '(无)'}\n   紧急程度: {urgency}"

    except FileNotFoundError:
        return "❌ 通知发送失败\n   原因: 系统中未找到 notify-send\n   解决方案: sudo apt install libnotify-bin"
    except subprocess.TimeoutExpired:
        return "❌ 通知发送失败\n   原因: notify-send 无响应（超时）\n   标题: {summary}"
    except Exception as e:
        return f"❌ 通知发送失败\n   原因: {e}\n   标题: {summary}"


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "send_notification",
            "description": "发送桌面通知。通过 notify-send 在系统桌面上显示通知消息，支持设置紧急程度、图标和显示时长。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "通知标题"
                    },
                    "body": {
                        "type": "string",
                        "description": "通知正文（可选）",
                        "default": ""
                    },
                    "urgency": {
                        "type": "string",
                        "description": "紧急程度：low（低）、normal（普通）、critical（紧急）",
                        "enum": ["low", "normal", "critical"],
                        "default": "normal"
                    },
                    "expire_time": {
                        "type": "integer",
                        "description": "显示时长（毫秒），设为 0 则永久显示",
                        "default": 5000
                    },
                    "icon": {
                        "type": "string",
                        "description": "图标名称或路径，例如 dialog-information",
                        "default": ""
                    }
                },
                "required": ["summary"]
            }
        }
    },
]
