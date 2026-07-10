"""Shared module-level state for cross-module access (bash session, file read state)."""

import os
from typing import Any, Dict, Optional


class _BashState:
    """Mutable bash session state shared between bash.py and subagent.py."""
    def __init__(self):
        # Compute project root: tool_registry/_state.py → parent is project root
        self.default_cwd: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cwd: str = self.default_cwd
        # Dict-based session storage (future: multi-session support).
        # The .session attribute is preserved as a backward-compat shortcut
        # that delegates to _sessions["default"].
        self._sessions: Dict[str, Any] = {}
        # Per-session working directories.
        self._cwds: Dict[str, str] = {}

    @property
    def session(self):
        return self._sessions.get("default")

    @session.setter
    def session(self, val):
        if val is None:
            self._sessions.pop("default", None)
        else:
            self._sessions["default"] = val

    def get_cwd(self, key: str) -> str:
        """Return the working directory for a given session key."""
        return self._cwds.get(key, self.default_cwd)

    def set_cwd(self, key: str, path: str):
        """Set the working directory for a given session key."""
        self._cwds[key] = path
        self.cwd = path


class _FileReadState:
    """Tracks full file reads for edit_file staleness validation."""
    def __init__(self):
        self.store: Dict[str, Dict[str, Any]] = {}


bash = _BashState()
file_read = _FileReadState()
