"""Skill Tool Executor — Tool for reading specialized agent skills.
"""

import logging
from typing import Any, Dict, List, Optional
from stores.skill_store import SkillStore

logger = logging.getLogger(__name__)

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "Load and read the full instruction guide for a specialized skill by its name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill to read (matching name in available_skills list)."
                    }
                },
                "required": ["skill_name"]
            }
        }
    }
]


import os


def execute_read_skill(skill_name: str, cancel_event=None) -> str:
    """Execute read_skill tool call."""
    from .bash import get_bash_cwd
    cwd = get_bash_cwd()
    store = SkillStore()

    detail = store.get_skill_detail(skill_name, cwd=cwd)
    if not detail:
        available = store.get_skills(cwd=cwd)
        names = [s.name for s in available]
        return f"❌ 找不到名为「{skill_name}」的 Skill。当前可用 Skill: {names if names else '无'}"

    skill_path, content, meta = detail
    base_dir = os.path.dirname(skill_path)

    header_lines = [
        f"📖 Skill 指南「{skill_name}」:",
        f"- 绝对路径: {skill_path}",
        f"- 关联根目录: {base_dir}",
    ]
    if meta.license:
        header_lines.append(f"- 许可协议: {meta.license}")
    if meta.compatibility:
        header_lines.append(f"- 兼容要求: {meta.compatibility}")
    if meta.resources:
        header_lines.append("- 附属资源文件:")
        for category, files in meta.resources.items():
            for f in files:
                header_lines.append(f"  * {f}")

    header_str = "\n".join(header_lines)

    return (
        f"{header_str}\n\n"
        f"----\n\n"
        f"{content}"
    )
