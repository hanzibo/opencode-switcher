"""Settings dialog — tabbed configuration window base framework.

Extensible Gtk.Notebook-based settings dialog. Tabs are organized as mixin
modules in dialogs/settings/ and loaded into the notebook registry.
"""

from typing import Optional, Callable

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk

from stores.clipboard_store import QQMailCredentialsStore, GmailOAuthStore, AISettingsStore
from ai_text_utils import set_code_highlight

from .tab_mail import MailTabMixin
from .tab_ai import AITabMixin
from .tab_prompts import PromptsTabMixin
from .tab_streaming import StreamingTabMixin
from .tab_theme import ThemeTabMixin
from .tab_constants import ConstantsTabMixin
from .tab_mcp import MCPTabMixin


def show_settings_dialog(parent_window: Gtk.Window,
                         ai_settings_store: Optional[AISettingsStore] = None,
                         on_dialog_shown: Optional[Callable[[], None]] = None,
                         on_dialog_hidden: Optional[Callable[[], None]] = None,
                         on_settings_saved: Optional[Callable[[], None]] = None,
                         current_theme: str = "dark",
                         on_theme_changed: Optional[Callable[[str], None]] = None):
    """Factory: create and show the Settings dialog.

    Parameters
    ----------
    current_theme : str
        Current theme name (``"dark"``, ``"light"``, or ``"dark-moon"``).
    on_theme_changed : callable, optional
        Called with the new theme name when the user changes and saves theme.
    """
    SettingsDialog(parent_window, ai_settings_store, on_dialog_shown,
                   on_dialog_hidden, on_settings_saved,
                   current_theme, on_theme_changed)


class SettingsDialog(
    MailTabMixin,
    AITabMixin,
    PromptsTabMixin,
    StreamingTabMixin,
    ThemeTabMixin,
    ConstantsTabMixin,
    MCPTabMixin,
):
    """Tabbed settings window.

    Tabs are defined in self._tabs as (name, builder_method) pairs.
    Add a new tab by appending to the list — the Notebook is built
    iteratively in build_ui().
    """

    def __init__(self, parent_window: Gtk.Window,
                 ai_settings_store: Optional[AISettingsStore] = None,
                 on_dialog_shown: Optional[Callable[[], None]] = None,
                 on_dialog_hidden: Optional[Callable[[], None]] = None,
                 on_settings_saved: Optional[Callable[[], None]] = None,
                 current_theme: str = "dark",
                 on_theme_changed: Optional[Callable[[str], None]] = None):
        self.parent_window = parent_window
        self.on_dialog_shown = on_dialog_shown
        self.on_dialog_hidden = on_dialog_hidden
        self.on_settings_saved = on_settings_saved
        self._current_theme = current_theme
        self._on_theme_changed = on_theme_changed

        # ── Tab registry: extend here for future tabs ──
        self._tabs = [
            ("QQ邮箱", self._build_qq_mail_tab),
            ("Gmail", self._build_gmail_tab),
            ("AI 对话", self._build_ai_settings_tab),
            ("系统提示词", self._build_system_prompt_tab),
            ("润色提示词", self._build_polish_prompt_tab),
            ("流式输出", self._build_streaming_tab),
            ("MCP 服务器", self._build_mcp_tab),
            ("常量配置", self._build_constants_tab),
            ("主题", self._build_theme_tab),
        ]

        self._qq_store = QQMailCredentialsStore()
        self._gmail_store = GmailOAuthStore()
        self._ai_settings_store = ai_settings_store or AISettingsStore()
        self._dialog = None

        # Tab widget references populated during UI construction
        self._soft_spin = None
        self._trim_spin = None
        self._enable_summary_check = None
        self._summary_thresh_spin = None
        self._summary_max_spin = None
        self._summary_prompt_view = None
        self._system_prompt_view = None
        self._polish_prompt_view = None
        self._clip_max_spin = None
        self._tool_iter_spin = None
        self._incremental_tools_check = None
        self._show_tool_details_check = None
        self._code_highlight_check = None
        self._enable_global_skills_check = None
        self._skill_toggle_widgets = []
        self._tool_toggle_widgets = []
        self._mcp_server_widgets = []

        self.build_ui()

    # ── UI Construction ──────────────────────────────────────────────────

    def build_ui(self):
        dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        dialog.get_style_context().add_class("custom-dialog")
        provider = Gtk.CssProvider.new()
        provider.load_from_data(b"""
            .custom-dialog notebook,
            .custom-dialog notebook > stack,
            .custom-dialog notebook > header,
            .custom-dialog notebook tabs,
            .custom-dialog notebook tab,
            .custom-dialog scrolledwindow,
            .custom-dialog viewport,
            .custom-dialog viewport.frame,
            .custom-dialog scrolledwindow.frame,
            .custom-dialog scrolledwindow > border,
            .custom-dialog viewport > border,
            .custom-dialog scrolledwindow box,
            .custom-dialog viewport box,
            .custom-dialog notebook box,
            .custom-dialog notebook stack box,
            viewport,
            viewport.frame {
                background-color: transparent;
                border: none;
                outline: none;
                box-shadow: none;
            }
            .mcp-sidebar-row {
                padding: 6px 8px;
                border-radius: 6px;
                transition: background-color 150ms ease-in-out;
            }
            .mcp-sidebar-row:hover {
                background-color: rgba(128, 128, 128, 0.08);
            }
            .mcp-sidebar-row:selected {
                background-color: rgba(66, 133, 244, 0.16);
            }
        """)
        dialog.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        dialog.set_title("Settings")
        dialog.set_modal(True)
        dialog.set_default_size(740, 600)
        dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        dialog.set_resizable(True)
        dialog.set_transient_for(self.parent_window)
        self._dialog = dialog

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        dialog.add(vbox)

        # ── Title ──
        title_lbl = Gtk.Label.new()
        title_lbl.set_markup("<b>Settings</b>")
        title_lbl.set_xalign(0)
        title_lbl.set_margin_start(16)
        title_lbl.set_margin_top(12)
        title_lbl.set_margin_bottom(8)
        vbox.pack_start(title_lbl, False, False, 0)

        sep_top = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep_top, False, False, 0)

        # ── Notebook (tabs) ──
        self._notebook = Gtk.Notebook.new()
        self._notebook.set_show_border(False)
        vbox.pack_start(self._notebook, True, True, 0)

        for tab_name, builder in self._tabs:
            page = builder()
            self._notebook.append_page(page, Gtk.Label.new(tab_name))

        # ── Bottom buttons ──
        sep_bottom = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep_bottom, False, False, 0)

        bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        bottom_box.set_margin_top(8)
        bottom_box.set_margin_bottom(10)
        bottom_box.set_margin_end(16)

        cancel_btn = Gtk.Button.new_with_label("Cancel")
        cancel_btn.connect("clicked", lambda _: dialog.destroy())

        save_btn = Gtk.Button.new_with_label("Save")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", lambda _: self._on_save())

        bottom_box.pack_end(save_btn, False, False, 0)
        bottom_box.pack_end(cancel_btn, False, False, 0)
        vbox.pack_start(bottom_box, False, False, 0)

        # ── Focus guards ──
        dialog.connect("show", lambda *_: self.on_dialog_shown and self.on_dialog_shown())
        dialog.connect("destroy", lambda *_: self.on_dialog_hidden and self.on_dialog_hidden())

        dialog.show_all()

    @staticmethod
    def _make_tab_scrolled_window(vbox: Gtk.Box) -> Gtk.ScrolledWindow:
        sw = Gtk.ScrolledWindow.new()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_vexpand(True)
        sw.set_shadow_type(Gtk.ShadowType.NONE)
        sw.add(vbox)
        child = sw.get_child()
        if isinstance(child, Gtk.Viewport):
            child.set_shadow_type(Gtk.ShadowType.NONE)
        return sw

    # ── Save logic ──────────────────────────────────────────────────────

    def _on_save(self):
        """Persist all settings and close the dialog."""
        # QQ Mail credentials
        if getattr(self, "_email_entry", None) is not None:
            self._qq_store.email = self._email_entry.get_text().strip()
            self._qq_store.auth_code = self._auth_entry.get_text().strip()
            self._qq_store.max_body_chars = int(self._body_chars_spin.get_value())
            self._qq_store.save()

        # AI 对话设置
        if self._soft_spin is not None:
            self._ai_settings_store.soft_limit = int(self._soft_spin.get_value())
            self._ai_settings_store.trim_target = int(self._trim_spin.get_value())
            self._ai_settings_store.enable_summary = self._enable_summary_check.get_active()
            self._ai_settings_store.summary_threshold = int(self._summary_thresh_spin.get_value())
            self._ai_settings_store.summary_max_chars = int(self._summary_max_spin.get_value())
            if self._summary_prompt_view is not None:
                buf = self._summary_prompt_view.get_buffer()
                self._ai_settings_store.summary_prompt_template = buf.get_text(
                    buf.get_start_iter(), buf.get_end_iter(), False
                )
        # 系统提示词（防御性判空：若标签页被移除/重排则不写入）
        sp_view = getattr(self, "_system_prompt_view", None)
        if sp_view is not None:
            sp_buf = sp_view.get_buffer()
            self._ai_settings_store.system_prompt = sp_buf.get_text(
                sp_buf.get_start_iter(), sp_buf.get_end_iter(), False
            )
        # 润色提示词
        pp_view = getattr(self, "_polish_prompt_view", None)
        if pp_view is not None:
            pp_buf = pp_view.get_buffer()
            self._ai_settings_store.polish_prompt_template = pp_buf.get_text(
                pp_buf.get_start_iter(), pp_buf.get_end_iter(), False
            )
        if self._clip_max_spin is not None:
            self._ai_settings_store.max_clipboard = int(self._clip_max_spin.get_value())
        if self._tool_iter_spin is not None:
            self._ai_settings_store.max_tool_iterations = int(self._tool_iter_spin.get_value())
        # 流式输出设置（始终为 full 模式，仅保留增量工具和详情选项）
        if self._incremental_tools_check is not None:
            self._ai_settings_store.enable_incremental_tools = self._incremental_tools_check.get_active()
            self._ai_settings_store.show_tool_details = self._show_tool_details_check.get_active()
            self._ai_settings_store.enable_code_highlight = self._code_highlight_check.get_active()
            set_code_highlight(self._ai_settings_store.enable_code_highlight)
            self._ai_settings_store.enable_global_skills = self._enable_global_skills_check.get_active()

        # 独立 Skill 使能开关
        disabled_skills = []
        for w in getattr(self, "_skill_toggle_widgets", []):
            if not w["check"].get_active():
                disabled_skills.append(w["name"])
        self._ai_settings_store.disabled_skills = disabled_skills

        # 内置工具开关
        disabled = []
        for w in getattr(self, "_tool_toggle_widgets", []):
            if not w["check"].get_active():
                disabled.append(w["name"])
        self._ai_settings_store.disabled_tools = disabled

        # MCP 服务器配置
        mcp_servers = []
        for w in getattr(self, "_mcp_server_widgets", []):
            name = w["name"].get_text().strip()
            if not name:
                continue
            transport = w["transport"].get_active_id()
            args_text = w["args"].get_text().strip()
            args_list = args_text.split() if args_text else []
            url = w["url"].get_text().strip()
            api_key = w["api_key"].get_text().strip()
            auth_type = w["auth_type"].get_active_id() if w["auth_type"].get_active_id() else "bearer"
            proto_ver = w["protocol_version"].get_active_id() if w["protocol_version"].get_active_id() else "2025-11-25"
            enable_2026 = w["enable_2026_headers"].get_active()
            oauth_client_id = w.get("oauth_client_id").get_text().strip() if w.get("oauth_client_id") else ""
            oauth_client_secret = w.get("oauth_client_secret").get_text().strip() if w.get("oauth_client_secret") else ""
            oauth_token_url = w.get("oauth_token_url").get_text().strip() if w.get("oauth_token_url") else ""
            oauth_scopes = w.get("oauth_scopes").get_text().strip() if w.get("oauth_scopes") else ""
            cwd_val = w["cwd"].get_text().strip() or None if w.get("cwd") else None
            env_dict = self._parse_env_str(w["env"].get_text().strip()) if w.get("env") else {}
            mcp_servers.append({
                "name": name,
                "transport": transport,
                "command": w["command"].get_text().strip() if transport == "stdio" else "",
                "args": args_list if transport == "stdio" else [],
                "cwd": cwd_val if transport == "stdio" else None,
                "env": env_dict if transport == "stdio" else {},
                "url": url,
                "api_key": api_key,
                "auth_type": auth_type,
                "oauth_client_id": oauth_client_id,
                "oauth_client_secret": oauth_client_secret,
                "oauth_token_url": oauth_token_url,
                "oauth_scopes": oauth_scopes,
                "protocol_version": proto_ver,
                "enable_2026_headers": enable_2026,
                "enabled": w["enabled"].get_active(),
                "auto_connect": w["auto_connect"].get_active(),
            })
        self._ai_settings_store.mcp_servers = mcp_servers
        self._ai_settings_store.save()

        # 主题设置
        if self._theme_light_radio.get_active():
            new_theme = "light"
        elif self._theme_dark_moon_radio.get_active():
            new_theme = "dark-moon"
        else:
            new_theme = "dark"

        if new_theme != self._current_theme:
            from stores.theme_config import save_theme_config
            save_theme_config(new_theme)
            if self._on_theme_changed:
                self._on_theme_changed(new_theme)

        if self.on_settings_saved:
            self.on_settings_saved()

        if self._dialog:
            self._dialog.destroy()
