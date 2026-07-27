"""Skill Tool Executor — Tool for reading specialized agent skills.
"""

import logging
from typing import Any, Dict, List, Optional
from skill_store import SkillStore

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

    skill_path, content = detail
    base_dir = os.path.dirname(skill_path)

    return (
        f"📖 Skill 指南「{skill_name}」:\n"
        f"- 绝对路径: {skill_path}\n"
        f"- 关联根目录: {base_dir}\n\n"
        f"----\n\n"
        f"{content}"
    )
