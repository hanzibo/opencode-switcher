"""Settings dialog backward-compatibility facade.

This module re-exports SettingsDialog and show_settings_dialog from
the modularized dialogs.settings package for 100% backward compatibility.
"""

from dialogs.settings import SettingsDialog, show_settings_dialog

__all__ = ["SettingsDialog", "show_settings_dialog"]
