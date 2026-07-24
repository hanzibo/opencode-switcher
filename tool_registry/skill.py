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


def execute_read_skill(skill_name: str, cancel_event=None) -> str:
    """Execute read_skill tool call."""
    from .bash import get_bash_cwd
    cwd = get_bash_cwd()
    store = SkillStore()

    content = store.get_skill_content(skill_name, cwd=cwd)
    if not content:
        available = store.get_skills(cwd=cwd)
        names = [s.name for s in available]
        return f"❌ 找不到名为「{skill_name}」的 Skill。当前可用 Skill: {names if names else '无'}"

    return f"📖 Skill 指南「{skill_name}」:\n\n{content}"
