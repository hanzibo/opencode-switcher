"""Shared module-level state for cross-module access (bash session, file read state)."""

import os
from typing import Any, Dict, Optional


class _BashState:
    """Mutable bash session state shared between bash.py and subagent.py."""
    def __init__(self):
        # Compute project root: tool_registry/_state.py → parent is project root
        self.default_cwd: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cwd: str = self.default_cwd
        self.session: Optional[Any] = None  # _BashSession instance, set by bash.py


class _FileReadState:
    """Tracks full file reads for edit_file staleness validation."""
    def __init__(self):
        self.store: Dict[str, Dict[str, Any]] = {}


bash = _BashState()
file_read = _FileReadState()
