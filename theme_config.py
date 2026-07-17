"""Centralized theme/color configuration for the entire app.

All theme colour values live here instead of being scattered across
panel.py, ai_chat_panel.py, ai_html_template.py, and chat.css.

Usage
-----
    from theme_config import get_theme, get_panel_css_vals

    theme = get_theme("light")
    vals = get_panel_css_vals("light")
"""

import json
import os
from typing import Dict, Any

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_DIR = os.path.expanduser("~/.config/opencode-switcher")

# ═══════════════════════════════════════════════════════════════════════════════
#  Color palette – all values live here, and only here.
#  Each dict is a complete theme; consumers extract what they need.
# ═══════════════════════════════════════════════════════════════════════════════

LIGHT: Dict[str, Any] = {
    # ── Search panel (panel.py) GTK colours ──
    "panel_bg":          (0.965, 0.973, 0.980, 1.0),    # #f6f8fa
    "panel_title":       (0.09,  0.09,  0.11,  1.0),    # near-black
    "panel_dir":         (0.39,  0.45,  0.55,  1.0),    # grey-blue
    "panel_snippet":     (0.39,  0.45,  0.55,  0.70),   # grey-blue 70%
    "panel_separator":   (0,     0,     0,     0.08),   # subtle grey
    "dot_live":          (0.020, 0.588, 0.412, 0.85),   # green
    "dot_recent":        (0.850, 0.467, 0.024, 0.75),   # amber
    "dot_closed":        (0.580, 0.639, 0.722, 0.4),    # grey

    # ── Panel CSS values (interpolated into CSS template) ──
    "window_border":     "rgba(0,0,0,0.06)",
    "hover_bg":          "rgba(0,0,0,0.05)",
    "sel_bg":            "rgba(79,70,229,0.10)",
    "sel_border":        "#4f46e5",
    "search_bg":         "#ffffff",
    "search_fg":         "#0f172a",
    "caret":             "#4f46e5",
    "input_border":      "rgba(0,0,0,0.10)",
    "tab_fg":            "rgba(15,23,42,0.55)",
    "tab_active_fg":     "#0f172a",
    "dialog_bg":         "#ffffff",
    "text_fg":           "#0f172a",
    "input_bg":          "#ffffff",
    "input_fg":          "#0f172a",
    "btn_bg":            "rgba(0,0,0,0.05)",
    "btn_border":        "rgba(0,0,0,0.10)",
    "btn_hover":         "rgba(0,0,0,0.09)",
    "btn_active":        "rgba(0,0,0,0.14)",

    # ── AI panel GTK widget colours (ai_chat_panel.py) ──
    "ai_bg":             (1.0,   1.0,   1.0,   1.0),    # #ffffff
    "ai_header_bg":      (0.965, 0.973, 0.980, 1.0),   # #f6f8fa
    "ai_input_bg":       (0.976, 0.980, 0.984, 1.0),   # #f9fafb

    # ── AI panel WebView CSS variables (ai_html_template.py) ──
    "web_bg":            "#ffffff",
    "web_text":          "rgba(15,23,42,0.92)",
    "web_pre_bg":        "rgba(0,0,0,0.04)",
    "web_code_bg":       "rgba(0,0,0,0.06)",
    "web_code_fg":       "#e11d48",
    "web_pre_border":    "rgba(0,0,0,0.12)",
    "web_thinking":      "#0284c7",
    "web_answer":        "#d97706",
    "web_user":          "#6366f1",
    "web_assistant":     "#0d9488",
    "web_table_header":  "rgba(0,0,0,0.06)",
    "web_table_alt":     "rgba(0,0,0,0.03)",
    "web_toggle":        "#0284c7",
}

DARK: Dict[str, Any] = {
    # ── Search panel (panel.py) GTK colours ──
    "panel_bg":          (0.039, 0.043, 0.063, 1.0),    # #0a0b10
    "panel_title":       (0.95,  0.96,  0.98,  1.0),    # near-white
    "panel_dir":         (0.39,  0.45,  0.55,  1.0),    # grey-blue
    "panel_snippet":     (0.28,  0.33,  0.41,  1.0),    # darker grey
    "panel_separator":   (1,     1,     1,     0.05),   # subtle white
    "dot_live":          (0.063, 0.725, 0.506, 0.9),    # green
    "dot_recent":        (0.960, 0.620, 0.043, 0.8),    # amber
    "dot_closed":        (0.392, 0.455, 0.545, 0.5),    # grey

    # ── Panel CSS values (interpolated into CSS template) ──
    "window_border":     "rgba(255,255,255,0.04)",
    "hover_bg":          "rgba(255,255,255,0.03)",
    "sel_bg":            "rgba(129,140,248,0.10)",
    "sel_border":        "#818cf8",
    "search_bg":         "#12131a",
    "search_fg":         "#f1f5f9",
    "caret":             "#818cf8",
    "input_border":      "rgba(255,255,255,0.06)",
    "tab_fg":            "rgba(255,255,255,0.45)",
    "tab_active_fg":     "#ffffff",
    "dialog_bg":         "#0a0b10",
    "text_fg":           "#f1f5f9",
    "input_bg":          "#12131a",
    "input_fg":          "#f1f5f9",
    "btn_bg":            "rgba(255,255,255,0.04)",
    "btn_border":        "rgba(255,255,255,0.06)",
    "btn_hover":         "rgba(129,140,248,0.12)",
    "btn_active":        "rgba(129,140,248,0.18)",

    # ── AI panel GTK widget colours (ai_chat_panel.py) ──
    "ai_bg":             (0.039, 0.043, 0.063, 1.0),    # #0a0b10
    "ai_header_bg":      (0.039, 0.043, 0.063, 1.0),    # same as bg for dark
    "ai_input_bg":       (0.039, 0.043, 0.063, 1.0),    # same as bg for dark

    # ── AI panel WebView CSS variables (ai_html_template.py) ──
    "web_bg":            "#0a0b10",
    "web_text":          "rgba(255,255,255,0.95)",
    "web_pre_bg":        "#12131a",
    "web_code_bg":       "rgba(255,255,255,0.06)",
    "web_code_fg":       "#f43f5e",
    "web_pre_border":    "rgba(255,255,255,0.08)",
    "web_thinking":      "#38bdf8",
    "web_answer":        "#f59e0b",
    "web_user":          "#818cf8",
    "web_assistant":     "#2dd4bf",
    "web_table_header":  "rgba(255,255,255,0.06)",
    "web_table_alt":     "rgba(255,255,255,0.03)",
    "web_toggle":        "#38bdf8",
}


# ── Lookup ────────────────────────────────────────────────────────────────────

_THEMES = {"light": LIGHT, "dark": DARK}


def get_theme(name: str) -> dict:
    """Return the full colour dict for the given theme name."""
    return _THEMES.get(name, LIGHT)


def get_panel_css_vals(name: str) -> dict:
    """Return the CSS-interpolation dict used by panel.py ``_set_theme``.

    Keys match the ``%(key)s`` placeholders in the panel CSS template.
    """
    t = get_theme(name)
    return {
        "window_border": t["window_border"],
        "hover_bg":      t["hover_bg"],
        "sel_bg":        t["sel_bg"],
        "sel_border":    t["sel_border"],
        "search_bg":     t["search_bg"],
        "search_fg":     t["search_fg"],
        "caret":         t["caret"],
        "input_border":  t["input_border"],
        "tab_fg":        t["tab_fg"],
        "tab_active_fg": t["tab_active_fg"],
        "dialog_bg":     t["dialog_bg"],
        "text_fg":       t["text_fg"],
        "input_bg":      t["input_bg"],
        "input_fg":      t["input_fg"],
        "btn_bg":        t["btn_bg"],
        "btn_border":    t["btn_border"],
        "btn_hover":     t["btn_hover"],
        "btn_active":    t["btn_active"],
    }


def get_web_css_vars(name: str) -> dict:
    """Return the CSS-variable dict used by ``get_html_template()``."""
    t = get_theme(name)
    return {
        "bg_color":        t["web_bg"],
        "text_color":      t["web_text"],
        "pre_bg":          t["web_pre_bg"],
        "code_bg":         t["web_code_bg"],
        "code_fg":         t["web_code_fg"],
        "pre_border":      t["web_pre_border"],
        "thinking_color":  t["web_thinking"],
        "answer_color":    t["web_answer"],
        "user_color":      t["web_user"],
        "assistant_color": t["web_assistant"],
        "table_header_bg": t["web_table_header"],
        "table_alt_bg":    t["web_table_alt"],
        "toggle_color":    t["web_toggle"],
    }


def get_ai_gtk_colors(name: str) -> dict:
    """Return GTK RGBA tuples for AI panel widgets."""
    t = get_theme(name)
    return {
        "bg":        t["ai_bg"],
        "header_bg": t["ai_header_bg"],
        "input_bg":  t["ai_input_bg"],
    }


# ── Persistence (theme choice) ────────────────────────────────────────────────

def load_theme_config() -> str:
    """Return the persisted theme name (``"dark"`` or ``"light"``).

    Falls back to ``"dark"`` when the config file is missing or corrupt.
    """
    cfg_path = os.path.join(CONFIG_DIR, "config.json")
    try:
        with open(cfg_path) as f:
            return json.load(f).get("theme", "dark")
    except Exception:
        return "dark"


def save_theme_config(theme_name: str):
    """Persist the chosen theme to ``config.json``.

    Called by both the tray menu (main.py) and the Settings dialog.
    """
    cfg_path = os.path.join(CONFIG_DIR, "config.json")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg["theme"] = theme_name
    try:
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
    except Exception as e:
        print(f"Error saving theme: {e}", flush=True)
