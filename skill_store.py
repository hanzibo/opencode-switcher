"""Skill Store — Agent Skill Discovery, Frontmatter Parsing, and Prompt Summary.

Manages standard SKILL.md skills across global (~/.config/opencode-switcher/skills/)
and project-local (.opencode/skills/ or .gemini/skills/) directories.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional


@dataclass
class SkillMetadata:
    """Metadata representing a single discovered Skill."""
    name: str
    description: str
    path: str
    allowed_tools: List[str] = field(default_factory=list)


def _parse_frontmatter(content: str) -> tuple[Dict[str, str], str]:
    """Parse YAML-style frontmatter enclosed in --- delimiters.

    Returns:
        (metadata_dict, body_text)
    """
    metadata: Dict[str, str] = {}
    pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return metadata, content.strip()

    front_text = match.group(1)
    body_text = match.group(2).strip()

    for line in front_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip().lower()
            val = val.strip().strip("\"'")
            if key:
                metadata[key] = val

    return metadata, body_text


def _parse_skill_file(file_path: str) -> Optional[SkillMetadata]:
    """Parse a single SKILL.md file and return SkillMetadata."""
    try:
        if not os.path.isfile(file_path):
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        meta, _ = _parse_frontmatter(content)

        # Fallback name to directory name or filename stem if missing
        dir_name = os.path.basename(os.path.dirname(file_path))
        if dir_name and dir_name != "skills":
            default_name = dir_name
        else:
            default_name = os.path.splitext(os.path.basename(file_path))[0]

        name = meta.get("name", default_name)
        description = meta.get("description", f"Skill instruction for {name}")

        allowed_tools_str = meta.get("allowed-tools", meta.get("allowed_tools", "")).strip("[] ")
        allowed_tools = [t.strip().strip("\"'") for t in allowed_tools_str.split(",") if t.strip()]

        return SkillMetadata(
            name=name,
            description=description,
            path=os.path.abspath(file_path),
            allowed_tools=allowed_tools
        )
    except Exception:
        return None


class SkillStore:
    """Discovers and provides access to standard agent skills."""

    _cache: Dict[str, tuple] = {}
    _TTL_SECONDS: float = 2.0

    def __init__(self, global_dir: Optional[Any] = None):
        if isinstance(global_dir, str):
            self.global_dirs = [global_dir]
        elif isinstance(global_dir, list):
            self.global_dirs = global_dir
        else:
            self.global_dirs = [
                os.path.expanduser("~/.config/opencode-switcher/skills"),
                os.path.expanduser("~/.agents/skills"),
                os.path.expanduser("~/.config/opencode/skills"),
            ]

    @property
    def global_dir(self) -> str:
        return self.global_dirs[0] if self.global_dirs else ""

    def _get_skill_directories(self, cwd: Optional[str] = None) -> List[str]:
        dirs = list(self.global_dirs)
        if cwd:
            dirs.append(os.path.join(cwd, ".opencode", "skills"))
            dirs.append(os.path.join(cwd, ".gemini", "skills"))
        return dirs

    def get_skills(self, cwd: Optional[str] = None) -> List[SkillMetadata]:
        """Scan global and project-local directories for SKILL.md files.

        Project-local skills override global skills with the same name.
        Uses a 2-second TTL cache per cwd to avoid excessive disk scans.
        """
        import time
        cache_key = f"{','.join(self.global_dirs)}:{cwd or ''}"
        now = time.time()

        if cache_key in self._cache:
            ts, cached_skills = self._cache[cache_key]
            if now - ts < self._TTL_SECONDS:
                return cached_skills

        skills_by_name: Dict[str, SkillMetadata] = {}

        for base_dir in self._get_skill_directories(cwd):
            if not os.path.exists(base_dir):
                continue

            # 1. Direct SKILL.md under subdirectories: base_dir/*/SKILL.md
            try:
                for entry in os.scandir(base_dir):
                    if entry.is_dir():
                        skill_file = os.path.join(entry.path, "SKILL.md")
                        if os.path.isfile(skill_file):
                            meta = _parse_skill_file(skill_file)
                            if meta:
                                skills_by_name[meta.name] = meta
                    elif entry.is_file() and entry.name.endswith(".md"):
                        # Support standalone <name>.md in skills directory
                        meta = _parse_skill_file(entry.path)
                        if meta:
                            skills_by_name[meta.name] = meta
            except Exception:
                continue

        result = list(skills_by_name.values())
        self._cache[cache_key] = (now, result)
        return result

    def get_skills_prompt_summary(self, cwd: Optional[str] = None) -> str:
        """Format discovered skills into XML structure for System Prompt injection."""
        skills = self.get_skills(cwd)
        if not skills:
            return ""

        lines = [
            "<available_skills>",
            "Below is a list of specialized skills available for this system.",
            "If the user's task matches a skill's description, call the 'read_skill' tool with the skill_name to view full instructions.",
        ]
        for sk in skills:
            lines.append("  <skill>")
            lines.append(f"    <name>{sk.name}</name>")
            lines.append(f"    <description>{sk.description}</description>")
            lines.append("  </skill>")
        lines.append("</available_skills>")

        return "\n".join(lines)

    def get_skill_content(self, skill_name: str, cwd: Optional[str] = None) -> Optional[str]:
        """Read and return full Markdown instructions of a skill by name."""
        skills = self.get_skills(cwd)
        for sk in skills:
            if sk.name == skill_name:
                try:
                    with open(sk.path, "r", encoding="utf-8") as f:
                        content = f.read()
                    _, body = _parse_frontmatter(content)
                    return body
                except Exception as e:
                    return f"Error reading skill '{skill_name}': {e}"
        return None
