"""Skill Store — Agent Skill Discovery, Frontmatter Parsing, and Prompt Summary.

Manages standard SKILL.md skills across global (~/.config/opencode-switcher/skills/)
and project-local (.opencode/skills/ or .gemini/skills/) directories.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional, Tuple


@dataclass
class SkillMetadata:
    """Metadata representing a single discovered Skill."""
    name: str
    description: str
    path: str
    allowed_tools: List[str] = field(default_factory=list)
    license: str = ""
    compatibility: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)
    resources: Dict[str, List[str]] = field(default_factory=dict)


def _scan_skill_resources(skill_file_path: str) -> Dict[str, List[str]]:
    """Scan subdirectories (scripts/, references/, assets/) relative to a SKILL.md file."""
    resources: Dict[str, List[str]] = {}
    parent_dir = os.path.dirname(os.path.abspath(skill_file_path))

    for sub in ["scripts", "references", "assets"]:
        sub_dir = os.path.join(parent_dir, sub)
        if os.path.isdir(sub_dir):
            try:
                found_files = []
                for root, _, files in os.walk(sub_dir):
                    for f in files:
                        if not f.startswith("."):
                            full_path = os.path.join(root, f)
                            rel_path = os.path.relpath(full_path, parent_dir)
                            found_files.append(rel_path)
                if found_files:
                    resources[sub] = sorted(found_files)
            except Exception:
                pass
    return resources


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

        license_val = meta.get("license", "")
        compatibility_val = meta.get("compatibility", "")

        custom_metadata: Dict[str, str] = {}
        standard_keys = {"name", "description", "allowed-tools", "allowed_tools", "license", "compatibility"}
        for k, v in meta.items():
            if k not in standard_keys:
                custom_metadata[k] = v

        resources = _scan_skill_resources(file_path)

        return SkillMetadata(
            name=name,
            description=description,
            path=os.path.abspath(file_path),
            allowed_tools=allowed_tools,
            license=license_val,
            compatibility=compatibility_val,
            metadata=custom_metadata,
            resources=resources,
        )
    except Exception:
        return None


class SkillStore:
    """Discovers and provides access to standard agent skills."""

    _cache: Dict[str, tuple] = {}
    _TTL_SECONDS: float = 2.0

    def __init__(self, global_dir: Optional[Any] = None, enable_global_skills: Optional[bool] = None, disabled_skills: Optional[List[str]] = None):
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

        if enable_global_skills is not None:
            self.enable_global_skills = enable_global_skills
        else:
            try:
                from clipboard_store import AISettingsStore
                self.enable_global_skills = AISettingsStore().enable_global_skills
            except Exception:
                self.enable_global_skills = True

        if disabled_skills is not None:
            self.disabled_skills = set(disabled_skills)
        else:
            try:
                from clipboard_store import AISettingsStore
                self.disabled_skills = set(AISettingsStore().disabled_skills)
            except Exception:
                self.disabled_skills = set()

    @property
    def global_dir(self) -> str:
        return self.global_dirs[0] if self.global_dirs else ""

    def _get_skill_directories(self, cwd: Optional[str] = None) -> List[str]:
        dirs = list(self.global_dirs) if self.enable_global_skills else []
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
        disabled_hash = ",".join(sorted(self.disabled_skills))
        cache_key = f"{','.join(self.global_dirs)}:{cwd or ''}:{disabled_hash}"
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
                            if meta and meta.name not in self.disabled_skills:
                                skills_by_name[meta.name] = meta
                    elif entry.is_file() and entry.name.endswith(".md"):
                        # Support standalone <name>.md in skills directory
                        meta = _parse_skill_file(entry.path)
                        if meta and meta.name not in self.disabled_skills:
                            skills_by_name[meta.name] = meta
            except Exception:
                continue

        result = list(skills_by_name.values())
        self._cache[cache_key] = (now, result)
        return result

    def get_skills_prompt_summary(self, cwd: Optional[str] = None) -> str:
        """Format discovered skills into an optimized XML structure for System Prompt injection."""
        skills = self.get_skills(cwd)
        if not skills:
            return ""

        lines = [
            "<available_skills>",
            "Below is a list of specialized skills available for this system.",
            "<instructions>",
            "When a user request matches a skill's description:",
            "1. Prioritize calling the 'read_skill' tool with the target skill_name BEFORE taking direct action.",
            "2. Strictly follow the retrieved skill instructions.",
            "</instructions>",
        ]
        for sk in skills:
            is_global = any(sk.path.startswith(g) for g in self.global_dirs if g)
            loc = "global" if is_global else "project"
            tools_str = ", ".join(sk.allowed_tools) if sk.allowed_tools else "all"

            lines.append("  <skill>")
            lines.append(f"    <name>{sk.name}</name>")
            lines.append(f"    <description>{sk.description}</description>")
            lines.append(f"    <location>{loc}</location>")
            lines.append(f"    <allowed_tools>{tools_str}</allowed_tools>")
            if sk.compatibility:
                lines.append(f"    <compatibility>{sk.compatibility}</compatibility>")
            if sk.resources:
                res_flat = []
                for _, files in sk.resources.items():
                    res_flat.extend(files)
                if res_flat:
                    lines.append(f"    <resources>{', '.join(res_flat)}</resources>")
            lines.append("  </skill>")
        lines.append("</available_skills>")

        return "\n".join(lines)

    def get_skill_detail(self, skill_name: str, cwd: Optional[str] = None) -> Optional[Tuple[str, str, SkillMetadata]]:
        """Read and return (absolute_path, markdown_body, metadata) of a skill by name."""
        skills = self.get_skills(cwd)
        for sk in skills:
            if sk.name == skill_name:
                try:
                    with open(sk.path, "r", encoding="utf-8") as f:
                        content = f.read()
                    _, body = _parse_frontmatter(content)
                    return (sk.path, body, sk)
                except Exception:
                    return None
        return None

    def get_skill_content(self, skill_name: str, cwd: Optional[str] = None) -> Optional[str]:
        """Read and return full Markdown instructions of a skill by name."""
        detail = self.get_skill_detail(skill_name, cwd)
        return detail[1] if detail else None
