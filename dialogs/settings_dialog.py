"""Settings dialog — tabbed configuration window.

Extensible Gtk.Notebook-based settings dialog.  Start with a QQ Mail
credentials tab, add more tabs by appending to the _tabs registry.

Pattern references:
  - sort_cats_dialog.py      → Gtk.Notebook usage
  - prompts_config_dialog.py → API-key visibility toggle
  - sort_cats_dialog.py      → custom-dialog + focus guards
"""

import html
import os
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

from typing import Optional, Callable

import threading

from stores.clipboard_store import QQMailCredentialsStore, GmailOAuthStore, AISettingsStore
from ai_text_utils import set_code_highlight
from mcp_integration import MCPServerConfig, MCPClientManager


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
        Current theme name (``"dark"`` or ``"light"``) for the Theme tab.
    on_theme_changed : callable, optional
        Called with the new theme name when the user changes and saves theme.
    """
    SettingsDialog(parent_window, ai_settings_store, on_dialog_shown,
                   on_dialog_hidden, on_settings_saved,
                   current_theme, on_theme_changed)


class SettingsDialog:
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

    # ── Tab: QQ Mail ─────────────────────────────────────────────────────

    def _build_qq_mail_tab(self):
        """Build the QQ Mail credentials tab page.

        Returns a Gtk.ScrolledWindow ready for notebook.append_page().
        """
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)

        # ── Email field ──
        email_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        email_lbl = Gtk.Label.new("邮箱地址:")
        email_lbl.set_size_request(90, -1)
        email_lbl.set_xalign(0)
        self._email_entry = Gtk.Entry.new()
        self._email_entry.set_placeholder_text("yourname@qq.com")
        self._email_entry.set_hexpand(True)
        email_hbox.pack_start(email_lbl, False, False, 0)
        email_hbox.pack_start(self._email_entry, True, True, 0)
        vbox.pack_start(email_hbox, False, False, 0)

        # ── Auth code field (with visibility toggle) ──
        auth_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        auth_lbl = Gtk.Label.new("授权码:")
        auth_lbl.set_size_request(90, -1)
        auth_lbl.set_xalign(0)
        self._auth_entry = Gtk.Entry.new()
        self._auth_entry.set_visibility(False)       # masked by default
        self._auth_entry.set_hexpand(True)

        show_auth_btn = Gtk.Button.new_with_label("显示")
        def on_show_auth_clicked(_btn):
            visible = self._auth_entry.get_visibility()
            self._auth_entry.set_visibility(not visible)
            show_auth_btn.set_label("隐藏" if not visible else "显示")
        show_auth_btn.connect("clicked", on_show_auth_clicked)

        auth_hbox.pack_start(auth_lbl, False, False, 0)
        auth_hbox.pack_start(self._auth_entry, True, True, 0)
        auth_hbox.pack_start(show_auth_btn, False, False, 0)
        vbox.pack_start(auth_hbox, False, False, 0)

        # ── Pre-fill from store ──
        self._email_entry.set_text(self._qq_store.email)
        self._auth_entry.set_text(self._qq_store.auth_code)

        # ── Max body chars ──
        chars_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        chars_hbox.set_margin_top(12)
        chars_lbl = Gtk.Label.new("正文截断长度:")
        chars_lbl.set_size_request(90, -1)
        chars_lbl.set_xalign(0)
        self._body_chars_spin = Gtk.SpinButton.new_with_range(100, 10000, 100)
        self._body_chars_spin.set_value(self._qq_store.max_body_chars)
        chars_hint = Gtk.Label.new("字符")
        chars_hint.set_opacity(0.6)
        chars_hbox.pack_start(chars_lbl, False, False, 0)
        chars_hbox.pack_start(self._body_chars_spin, False, False, 0)
        chars_hbox.pack_start(chars_hint, False, False, 0)
        vbox.pack_start(chars_hbox, False, False, 0)

        # ── Help hint ──
        help_frame = Gtk.Frame.new()
        help_frame.set_margin_top(16)

        help_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 4)
        help_vbox.set_margin_start(10)
        help_vbox.set_margin_end(10)
        help_vbox.set_margin_top(10)
        help_vbox.set_margin_bottom(10)

        help_title = Gtk.Label.new()
        help_title.set_markup("<b>📌 如何获取授权码？</b>")
        help_title.set_xalign(0)
        help_vbox.pack_start(help_title, False, False, 0)

        for line in [
            "1. 登录 QQ邮箱网页版 → 设置 → 账号与安全",
            "2. 开启「POP3/SMTP/IMAP 服务」（需短信验证）",
            "3. 验证成功后获取 16 位授权码",
            "4. 将授权码填入上方「授权码」输入框即可",
        ]:
            lbl = Gtk.Label.new(line)
            lbl.set_xalign(0)
            lbl.set_margin_start(4)
            help_vbox.pack_start(lbl, False, False, 0)

        help_frame.add(help_vbox)
        vbox.pack_start(help_frame, False, False, 0)

        # ── Spacer so content stays top-aligned ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return self._make_tab_scrolled_window(vbox)

    # ── Tab: Gmail ─────────────────────────────────────────────────────

    def _build_gmail_tab(self):
        """Build the Gmail OAuth 2.0 authorization tab page.

        Returns a Gtk.ScrolledWindow ready for notebook.append_page().
        """
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)

        # ── Status display ──
        status_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        status_lbl = Gtk.Label.new()
        status_lbl.set_markup("<b>授权状态</b>")
        status_lbl.set_size_request(90, -1)
        status_lbl.set_xalign(0)

        self._gmail_status_label = Gtk.Label.new()
        self._gmail_status_label.set_xalign(0)
        self._gmail_status_label.set_hexpand(True)
        self._update_gmail_status_ui()

        status_hbox.pack_start(status_lbl, False, False, 0)
        status_hbox.pack_start(self._gmail_status_label, True, True, 0)
        vbox.pack_start(status_hbox, False, False, 0)

        # ── Action buttons ──
        btn_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        btn_hbox.set_margin_top(12)

        self._gmail_auth_btn = Gtk.Button.new_with_label("📡 登录 Google 账号进行授权")
        self._gmail_auth_btn.connect("clicked", self._on_gmail_authorize)
        btn_hbox.pack_start(self._gmail_auth_btn, False, False, 0)

        self._gmail_revoke_btn = Gtk.Button.new_with_label("🗑️ 撤销授权")
        self._gmail_revoke_btn.connect("clicked", self._on_gmail_revoke)
        btn_hbox.pack_start(self._gmail_revoke_btn, False, False, 0)
        self._gmail_revoke_btn.set_sensitive(self._gmail_store.is_authorized)

        vbox.pack_start(btn_hbox, False, False, 0)

        # ── Help instructions ──
        help_frame = Gtk.Frame.new()
        help_frame.set_margin_top(20)

        help_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 4)
        help_vbox.set_margin_start(10)
        help_vbox.set_margin_end(10)
        help_vbox.set_margin_top(10)
        help_vbox.set_margin_bottom(10)

        help_title = Gtk.Label.new()
        help_title.set_markup("<b>📖 首次使用说明</b>")
        help_title.set_xalign(0)
        help_vbox.pack_start(help_title, False, False, 0)

        creds_path = os.path.expanduser(
            "~/.config/opencode-switcher/gmail_credentials/credentials.json"
        )
        for line in [
            "Gmail 工具使用 Google 官方 API + OAuth 2.0 认证，首次使用前需完成以下步骤：",
            "",
            "1. 访问 https://console.cloud.google.com/",
            "2. 新建项目（或选择已有项目）",
            "3. 导航到「API 和服务」→「库」，搜索并启用「Gmail API」",
            "4. 导航到「API 和服务」→「OAuth 同意屏幕」",
            "   - User Type 选择「External」（即使内部使用）",
            "   - 填写 App name、用户支持邮箱",
            "   - 在「范围 (Scopes)」页面添加 Gmail API → /auth/gmail.readonly",
            "   - 在「测试用户」页面添加你自己的 Gmail 地址",
            "5. 导航到「API 和服务」→「凭据」→「创建凭据」→「OAuth 客户端 ID」",
            "   - 应用类型选「桌面应用」",
            "   - 下载 JSON 文件",
            f"6. 将下载的 JSON 文件重命名为 credentials.json 放入：",
            f"   {creds_path}",
            "7. 回到本页面，点击上方「登录 Google 账号进行授权」按钮",
            "8. 浏览器弹出授权页面 → 登录你的 Gmail 账号 → 点击「允许」",
            "9. 授权完成后，状态将显示「已授权 xxx@gmail.com」",
            "",
            "授权后 token 会自动缓存，后续调用无需再次授权。",
            "如 token 过期，工具会自动刷新（无需手动操作）。",
        ]:
            lbl = Gtk.Label.new(line)
            lbl.set_xalign(0)
            lbl.set_margin_start(4)
            help_vbox.pack_start(lbl, False, False, 0)

        help_frame.add(help_vbox)
        vbox.pack_start(help_frame, False, False, 0)

        # ── Spacer ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return self._make_tab_scrolled_window(vbox)

    def _update_gmail_status_ui(self):
        """Refresh the Gmail authorization status label."""
        if self._gmail_store.is_authorized and self._gmail_store.email:
            self._gmail_status_label.set_markup(
                f"<span foreground='green'>✅ 已授权</span> {self._gmail_store.email}"
            )
        elif self._gmail_store.is_authorized:
            self._gmail_status_label.set_markup(
                "<span foreground='green'>✅ 已授权</span>"
            )
        else:
            self._gmail_status_label.set_markup(
                "<span foreground='red'>❌ 未授权</span> — "
                "请先完成下方首次使用说明中的步骤 1-6，然后点击上方按钮授权。"
            )

    def _on_gmail_authorize(self, button):
        """Start the OAuth 2.0 authorization flow in a background thread."""
        button.set_sensitive(False)
        button.set_label("授权中...")

        def _do_auth():
            try:
                from tool_registry.gmail import _get_credentials
                creds = _get_credentials()
                email = ""
                try:
                    if hasattr(creds, 'token_info') and creds.token_info:
                        email = creds.token_info.get('email', '')
                except Exception:
                    pass
                GLib.idle_add(lambda: self._on_gmail_auth_done(creds, email))
            except Exception as e:
                GLib.idle_add(lambda: self._on_gmail_auth_error(str(e)))

        threading.Thread(target=_do_auth, daemon=True).start()

    def _on_gmail_auth_done(self, creds, email: str):
        """Handle successful OAuth authorization.

        Token is already saved by _get_credentials()/_save_token()
        in gmail.py. Here we just reload store state and update UI.
        """
        self._gmail_store._load()
        if email and not self._gmail_store.email:
            self._gmail_store.email = email
        self._update_gmail_status_ui()
        self._gmail_auth_btn.set_sensitive(True)
        self._gmail_auth_btn.set_label("📡 登录 Google 账号进行授权")
        self._gmail_revoke_btn.set_sensitive(True)

    def _on_gmail_auth_error(self, error: str):
        """Handle OAuth authorization error."""
        self._gmail_store.is_authorized = False
        self._update_gmail_status_ui()
        self._gmail_auth_btn.set_sensitive(True)
        self._gmail_auth_btn.set_label("📡 登录 Google 账号进行授权")
        # Show error dialog
        dialog = Gtk.MessageDialog(
            transient_for=self._dialog,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Gmail 授权失败",
        )
        dialog.format_secondary_text(str(error))
        dialog.connect("response", lambda dlg, _: dlg.destroy())
        dialog.show_all()

    def _on_gmail_revoke(self, button):
        """Revoke Gmail authorization."""
        self._gmail_store.revoke()
        self._update_gmail_status_ui()
        self._gmail_revoke_btn.set_sensitive(False)

    # ── Tab: AI 对话 ───────────────────────────────────────────────────

    def _build_ai_settings_tab(self):
        """Build the AI conversation truncation settings tab page.

        Returns a Gtk.ScrolledWindow ready for notebook.append_page().
        """
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)

        # ── Soft limit (triggering threshold) ──
        soft_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        soft_lbl = Gtk.Label.new("触发截断的消息数:")
        soft_lbl.set_size_request(150, -1)
        soft_lbl.set_xalign(0)
        self._soft_spin = Gtk.SpinButton.new_with_range(50, 9999, 10)
        self._soft_spin.set_value(self._ai_settings_store.soft_limit)
        soft_hbox.pack_start(soft_lbl, False, False, 0)
        soft_hbox.pack_start(self._soft_spin, False, False, 0)
        vbox.pack_start(soft_hbox, False, False, 0)

        # ── Trim target ──
        trim_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        trim_lbl = Gtk.Label.new("裁剪后保留的消息数:")
        trim_lbl.set_size_request(150, -1)
        trim_lbl.set_xalign(0)
        self._trim_spin = Gtk.SpinButton.new_with_range(10, 400, 10)
        self._trim_spin.set_value(self._ai_settings_store.trim_target)
        trim_hbox.pack_start(trim_lbl, False, False, 0)
        trim_hbox.pack_start(self._trim_spin, False, False, 0)
        vbox.pack_start(trim_hbox, False, False, 0)

        # ── Help text ──
        hint = Gtk.Label.new()
        hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "当消息数超过「触发截断的消息数」时，自动裁剪到「裁剪后保留的消息数」。\n"
            "首条消息始终保留，从最旧的开始丢弃。"
            "</span>"
        )
        hint.set_xalign(0)
        hint.set_margin_top(12)
        vbox.pack_start(hint, False, False, 0)

        # ── Separator before summary compression settings ──
        sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(16)
        sep.set_margin_bottom(12)
        vbox.pack_start(sep, False, False, 0)

        # ── Summary compression section title ──
        summary_title = Gtk.Label.new()
        summary_title.set_markup("<b>📝 摘要压缩</b>")
        summary_title.set_xalign(0)
        vbox.pack_start(summary_title, False, False, 0)

        # ── Enable summary compression ──
        summary_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        summary_hbox.set_margin_top(8)
        self._enable_summary_check = Gtk.CheckButton.new_with_label("启用摘要压缩")
        self._enable_summary_check.set_active(self._ai_settings_store.enable_summary)
        summary_hbox.pack_start(self._enable_summary_check, False, False, 0)
        vbox.pack_start(summary_hbox, False, False, 0)

        # ── Summary threshold ──
        thresh_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        thresh_lbl = Gtk.Label.new("触发摘要的消息余量:")
        thresh_lbl.set_size_request(150, -1)
        thresh_lbl.set_xalign(0)
        self._summary_thresh_spin = Gtk.SpinButton.new_with_range(20, 300, 10)
        self._summary_thresh_spin.set_value(self._ai_settings_store.summary_threshold)
        thresh_hbox.pack_start(thresh_lbl, False, False, 0)
        thresh_hbox.pack_start(self._summary_thresh_spin, False, False, 0)
        vbox.pack_start(thresh_hbox, False, False, 0)

        # ── Summary max chars ──
        max_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        max_lbl = Gtk.Label.new("摘要最大字符数:")
        max_lbl.set_size_request(150, -1)
        max_lbl.set_xalign(0)
        self._summary_max_spin = Gtk.SpinButton.new_with_range(100, 2000, 100)
        self._summary_max_spin.set_value(self._ai_settings_store.summary_max_chars)
        max_hbox.pack_start(max_lbl, False, False, 0)
        max_hbox.pack_start(self._summary_max_spin, False, False, 0)
        vbox.pack_start(max_hbox, False, False, 0)

        # ── Summary prompt template ──
        prompt_lbl = Gtk.Label.new("摘要提示词模板（支持占位符）:")
        prompt_lbl.set_xalign(0)
        prompt_lbl.set_margin_top(12)
        vbox.pack_start(prompt_lbl, False, False, 0)

        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_min_content_height(120)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_shadow_type(Gtk.ShadowType.NONE)
        self._summary_prompt_view = Gtk.TextView.new()
        self._summary_prompt_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._summary_prompt_view.set_monospace(True)
        buffer = self._summary_prompt_view.get_buffer()
        buffer.set_text(self._ai_settings_store.summary_prompt_template)
        scrolled.add(self._summary_prompt_view)
        vbox.pack_start(scrolled, False, False, 0)

        prompt_hint = Gtk.Label.new()
        prompt_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "可用占位符："
            "{prev_summary} 已有摘要 / "
            "{conversation_text} 对话内容 / "
            "{max_chars} 最大字符数"
            "</span>"
        )
        prompt_hint.set_xalign(0)
        prompt_hint.set_margin_top(4)
        vbox.pack_start(prompt_hint, False, False, 0)

        # ── Help text for summary ──
        summary_hint = Gtk.Label.new()
        summary_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "启用后，当消息数超过阈值时，将最早的消息压缩为摘要而不是直接丢弃，"
            "保留关键信息。\n摘要会作为系统消息注入后续对话，帮助 Agent 记住早期内容。"
            "</span>"
        )
        summary_hint.set_xalign(0)
        summary_hint.set_margin_top(8)
        vbox.pack_start(summary_hint, False, False, 0)

        # ── Separator before code highlight ──
        hl_sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        hl_sep.set_margin_top(16)
        hl_sep.set_margin_bottom(12)
        vbox.pack_start(hl_sep, False, False, 0)

        # ── Code highlight section title ──
        hl_title = Gtk.Label.new()
        hl_title.set_markup("<b>🎨 代码渲染</b>")
        hl_title.set_xalign(0)
        vbox.pack_start(hl_title, False, False, 0)

        # ── Enable code highlight ──
        hl_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        hl_hbox.set_margin_top(8)
        self._code_highlight_check = Gtk.CheckButton.new_with_label("启用代码语法高亮（Pygments）")
        self._code_highlight_check.set_active(self._ai_settings_store.enable_code_highlight)
        hl_hbox.pack_start(self._code_highlight_check, False, False, 0)
        vbox.pack_start(hl_hbox, False, False, 0)

        hl_hint = Gtk.Label.new()
        hl_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "关闭后可降低渲染开销，对设备性能较弱的场景有明显改善。"
            "需要 Python 包 Pygments 支持。"
            "</span>"
        )
        hl_hint.set_xalign(0)
        hl_hint.set_margin_top(4)
        vbox.pack_start(hl_hint, False, False, 0)

        # ── Separator before Skill settings ──
        skill_sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        skill_sep.set_margin_top(16)
        skill_sep.set_margin_bottom(12)
        vbox.pack_start(skill_sep, False, False, 0)

        # ── Skill section title ──
        skill_title = Gtk.Label.new()
        skill_title.set_markup("<b>🎯 AI Skill 技能系统</b>")
        skill_title.set_xalign(0)
        vbox.pack_start(skill_title, False, False, 0)

        # ── Enable global skills CheckButton ──
        skill_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        skill_hbox.set_margin_top(8)
        self._enable_global_skills_check = Gtk.CheckButton.new_with_label("启用全局 Skill 自动发现 (~/.config/opencode-switcher/skills, ~/.agents/skills 等)")
        self._enable_global_skills_check.set_active(self._ai_settings_store.enable_global_skills)
        skill_hbox.pack_start(self._enable_global_skills_check, False, False, 0)
        vbox.pack_start(skill_hbox, False, False, 0)

        skill_hint = Gtk.Label.new()
        skill_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "关闭后 AI 助手将仅自动识别当前项目根路径（.opencode/skills 及 .gemini/skills）下的 Skill，"
            "屏蔽全局通用 Skill 目录。\n下方可对已识别到的每个 Skill 进行单独开启/关闭管理。"
            "</span>"
        )
        skill_hint.set_xalign(0)
        skill_hint.set_margin_top(4)
        vbox.pack_start(skill_hint, False, False, 0)

        # ── Per-Skill enable/disable toggle list ──
        self._build_skill_toggle_section(vbox)

        # ── Separator before built-in tool toggle ──
        tool_sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        tool_sep.set_margin_top(16)
        tool_sep.set_margin_bottom(12)
        vbox.pack_start(tool_sep, False, False, 0)

        # ── Built-in tool toggle section ──
        self._build_tool_toggle_section(vbox)

        # ── Spacer ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return self._make_tab_scrolled_window(vbox)

    def _build_skill_toggle_section(self, parent_vbox):
        """动态渲染每个已扫描到的 Skill 的独立使能开关。"""
        from stores.skill_store import SkillStore
        from tool_registry import get_bash_cwd

        cwd = get_bash_cwd()
        all_skills = SkillStore(disabled_skills=[]).get_skills(cwd=cwd)

        self._skill_toggle_widgets = []
        disabled_set = set(self._ai_settings_store.disabled_skills)

        if not all_skills:
            no_skills_lbl = Gtk.Label.new("（当前未扫描到任何已安装的 Skill）")
            no_skills_lbl.set_xalign(0)
            no_skills_lbl.set_opacity(0.6)
            no_skills_lbl.set_margin_top(6)
            parent_vbox.pack_start(no_skills_lbl, False, False, 0)
            return

        skill_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 4)
        skill_box.set_margin_top(8)

        global_dirs = SkillStore().global_dirs

        for sk in all_skills:
            hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
            check = Gtk.CheckButton.new_with_label(sk.name)
            check.set_active(sk.name not in disabled_set)

            is_global = any(sk.path.startswith(g) for g in global_dirs if g)
            tag_text = "[全局]" if is_global else "[项目]"
            tag_color = "#3b82f6" if is_global else "#10b981"

            desc_lbl = Gtk.Label.new()
            desc_lbl.set_markup(
                f"<span foreground='{tag_color}'><b>{tag_text}</b></span> "
                f"<span foreground='#888888'>{html.escape(sk.description)}</span>"
            )
            desc_lbl.set_xalign(0)
            desc_lbl.set_ellipsize(Pango.EllipsizeMode.END)

            hbox.pack_start(check, False, False, 0)
            hbox.pack_start(desc_lbl, True, True, 0)
            skill_box.pack_start(hbox, False, False, 0)

            self._skill_toggle_widgets.append({
                "name": sk.name,
                "check": check,
            })

        parent_vbox.pack_start(skill_box, False, False, 0)

    def _build_tool_toggle_section(self, parent_vbox):
        """Build the built-in tool enable/disable toggle section.

        按模块分组显示所有内置工具，每组可折叠，每个工具有独立开关。
        """
        from tool_registry import TOOL_DEFINITIONS, TOOL_MODULE_MAP

        # ── Section title ──
        title = Gtk.Label.new()
        title.set_markup("<b>🔧 内置工具开关</b>")
        title.set_xalign(0)
        parent_vbox.pack_start(title, False, False, 0)

        hint = Gtk.Label.new()
        hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "取消勾选可禁用对应内置工具，AI 将无法调用。"
            "MCP 工具不受此设置影响。"
            "</span>"
        )
        hint.set_xalign(0)
        hint.set_margin_top(4)
        parent_vbox.pack_start(hint, False, False, 0)

        # ── 解析工具按模块分组 ──
        groups: dict = {}
        risk_map = {
            "bash": "high", "write_file": "high", "edit_file": "high", "sub_agent": "high",
            "read_file": "medium", "grep_search": "medium",
            "web_search": "medium", "web_fetch": "medium",
            "read_qq_mail": "medium", "read_gmail_mail": "medium", "memory_save": "medium",
        }

        for s in TOOL_DEFINITIONS:
            name = s["function"]["name"]
            group = TOOL_MODULE_MAP.get(name, "other")
            groups.setdefault(group, []).append(s)

        # 组排序（按重要性）
        group_order = ["common", "todo", "filesystem", "search", "web", "bash",
                       "notification", "mail", "subagent", "code_analysis",
                       "memory", "skill"]
        group_labels = {
            "common": "🟢 通用", "todo": "🟢 任务管理",
            "filesystem": "🔴 文件系统", "search": "🟡 搜索",
            "web": "🟡 网页", "bash": "🔴 Shell",
            "notification": "🟢 通知", "mail": "🟡 邮件",
            "subagent": "🔴 子代理", "code_analysis": "🟢 代码分析",
            "memory": "🟡 记忆", "skill": "🟡 技能",
        }

        # 存储所有 checkbutton 以便保存时读取
        self._tool_toggle_widgets: list[dict] = []
        self._tool_toggle_lookup: dict[str, Gtk.CheckButton] = {}
        disabled_set = set(self._ai_settings_store.disabled_tools)

        # 每组一个可折叠区域
        expander = Gtk.Expander.new("全部工具 (点击展开/收起)")
        expander.set_expanded(False)
        expander.set_margin_top(8)

        inner_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 4)
        inner_box.set_margin_start(12)

        for g in group_order:
            schemas = groups.get(g, [])
            if not schemas:
                continue

            # ── 组标题（不可折叠，仅用于视觉分组） ──
            group_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
            group_box.set_margin_top(8)

            group_label_text = group_labels.get(g, g)
            group_lbl = Gtk.Label.new()
            group_lbl.set_markup(f"<b>{group_label_text}</b>")
            group_lbl.set_xalign(0)
            group_lbl.set_size_request(120, -1)
            group_box.pack_start(group_lbl, False, False, 0)

            # 全选/全不选按钮
            group_all_in = all(
                s["function"]["name"] not in disabled_set for s in schemas
            )
            group_check = Gtk.CheckButton.new_with_label("全选")
            group_check.set_active(group_all_in)
            group_check.connect("toggled", self._on_group_toggle, schemas, inner_box)
            group_box.pack_start(group_check, False, False, 0)

            inner_box.pack_start(group_box, False, False, 0)

            # ── 每个工具一个 checkbutton ──
            for s in schemas:
                name = s["function"]["name"]
                desc = s["function"]["description"][:60] + ("…" if len(s["function"]["description"]) > 60 else "")

                tool_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
                tool_hbox.set_margin_start(16)
                tool_hbox.set_margin_top(2)

                risk = risk_map.get(name, "low")
                risk_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(risk, "🟢")

                check = Gtk.CheckButton.new()
                check.set_active(name not in disabled_set)
                check.set_tooltip_text(f"{name}: {desc}")
                tool_hbox.pack_start(check, False, False, 0)

                name_lbl = Gtk.Label.new()
                name_lbl.set_markup(f"{risk_icon} {name}")
                name_lbl.set_xalign(0)
                name_lbl.set_size_request(220, -1)
                tool_hbox.pack_start(name_lbl, False, False, 0)

                desc_lbl = Gtk.Label.new()
                desc_lbl.set_markup(f"<span size='small' foreground='#888888'>{desc}</span>")
                desc_lbl.set_xalign(0)
                desc_lbl.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
                desc_lbl.set_size_request(300, -1)
                tool_hbox.pack_start(desc_lbl, False, False, 0)

                inner_box.pack_start(tool_hbox, False, False, 0)

                self._tool_toggle_widgets.append({
                    "name": name,
                    "check": check,
                    "group": g,
                })
                self._tool_toggle_lookup[name] = check

        expander.add(inner_box)
        parent_vbox.pack_start(expander, False, False, 0)

        # ── 底部操作按钮 ──
        btn_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        btn_box.set_margin_top(8)

        enable_all_btn = Gtk.Button.new_with_label("全部启用")
        enable_all_btn.connect("clicked", self._on_tool_enable_all)
        btn_box.pack_start(enable_all_btn, False, False, 0)

        disable_high_btn = Gtk.Button.new_with_label("禁用高风险")
        disable_high_btn.set_tooltip_text("一键禁用 bash、write_file、edit_file、sub_agent")
        disable_high_btn.connect("clicked", self._on_tool_disable_high_risk)
        btn_box.pack_start(disable_high_btn, False, False, 0)

        parent_vbox.pack_start(btn_box, False, False, 0)

    def _on_group_toggle(self, btn, schemas, inner_box):
        """组全选/全不选按钮切换时，同步组内所有工具。"""
        active = btn.get_active()
        for s in schemas:
            check = self._tool_toggle_lookup.get(s["function"]["name"])
            if check:
                check.set_active(active)

    def _on_tool_enable_all(self, btn):
        """全部启用按钮。"""
        for w in self._tool_toggle_widgets:
            w["check"].set_active(True)

    def _on_tool_disable_high_risk(self, btn):
        """禁用高风险工具。"""
        high_risk = {"bash", "write_file", "edit_file", "sub_agent"}
        for w in self._tool_toggle_widgets:
            if w["name"] in high_risk:
                w["check"].set_active(False)
            else:
                w["check"].set_active(True)

    # ── Tab: 系统提示词 ─────────────────────────────────────────────────

    def _build_system_prompt_tab(self):
        """Build the global AI system prompt configuration tab page.

        用户在此编写全局系统提示词（system prompt），仅在**新建立的 AI 对话**
        首轮请求时作为 system 消息注入。已存在的对话使用自身快照，不受此处
        修改影响（避免 LLM prompt 前缀变化导致缓存失效、浪费 token）。
        """
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)

        # ── Description ──
        desc = Gtk.Label.new()
        desc.set_markup(
            "<span size='small' foreground='#888888'>"
            "在此编写全局系统提示词（system prompt）。\n"
            "保存后，<b>新建的 AI 对话</b>将以此作为对话开头的 system 消息。\n"
            "已存在的对话保持创建时的快照，不受后续修改影响（保证请求前缀稳定，LLM 缓存可命中）。"
            "</span>"
        )
        desc.set_xalign(0)
        desc.set_line_wrap(True)
        vbox.pack_start(desc, False, False, 0)

        # ── Editor ──
        editor_title = Gtk.Label.new()
        editor_title.set_markup("<b>系统提示词内容</b>")
        editor_title.set_xalign(0)
        editor_title.set_margin_top(8)
        vbox.pack_start(editor_title, False, False, 0)

        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_min_content_height(220)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_shadow_type(Gtk.ShadowType.NONE)
        self._system_prompt_view = Gtk.TextView.new()
        self._system_prompt_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._system_prompt_view.set_monospace(True)
        buffer = self._system_prompt_view.get_buffer()
        buffer.set_text(self._ai_settings_store.system_prompt)
        scrolled.add(self._system_prompt_view)
        vbox.pack_start(scrolled, True, True, 0)

        # ── Hint ──
        hint = Gtk.Label.new()
        hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "留空表示不注入系统提示词（默认行为）。\n"
            "提示：可与「AI 对话 → 摘要压缩」的历史摘要共存，系统提示词会放在消息最前。"
            "</span>"
        )
        hint.set_xalign(0)
        hint.set_margin_top(8)
        vbox.pack_start(hint, False, False, 0)

        # ── Spacer ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return self._make_tab_scrolled_window(vbox)

    # ── Tab: 润色提示词 ─────────────────────────────────────────────────

    def _build_polish_prompt_tab(self):
        """Build the AI prompt polish template configuration tab page."""
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)

        # ── Description ──
        desc = Gtk.Label.new()
        desc.set_markup(
            "<span size='small' foreground='#888888'>"
            "在此自定义斜杠命令 <b>/ai-polish</b> 所使用的润色提示词模板。\n"
            "支持使用以下占位符，系统会在发起润色请求时自动识别并动态替换："
            "</span>"
        )
        desc.set_xalign(0)
        desc.set_line_wrap(True)
        vbox.pack_start(desc, False, False, 0)

        # ── Placeholder tags info ──
        tag_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 4)
        tag_box.set_margin_top(4)
        tag_box.set_margin_bottom(4)

        tag1 = Gtk.Label.new()
        tag1.set_markup(
            "• <span foreground='#3b82f6'><b>{model-last-answer}</b></span>: "
            "<span foreground='#aaaaaa'>替换为当前对话历史中模型最后一次正式回答内容（无历史回答时自动填充说明）</span>"
        )
        tag1.set_xalign(0)

        tag2 = Gtk.Label.new()
        tag2.set_markup(
            "• <span foreground='#10b981'><b>{user-original-message}</b></span>: "
            "<span foreground='#aaaaaa'>替换为用户在 /ai-polish 命令后输入的原始提问文本</span>"
        )
        tag2.set_xalign(0)

        tag_box.pack_start(tag1, False, False, 0)
        tag_box.pack_start(tag2, False, False, 0)
        vbox.pack_start(tag_box, False, False, 0)

        # ── Scrolled TextView for template ──
        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_min_content_height(220)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_shadow_type(Gtk.ShadowType.ETCHED_IN)

        self._polish_prompt_view = Gtk.TextView.new()
        self._polish_prompt_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._polish_prompt_view.set_monospace(True)
        buf = self._polish_prompt_view.get_buffer()
        buf.set_text(self._ai_settings_store.polish_prompt_template)
        scrolled.add(self._polish_prompt_view)
        vbox.pack_start(scrolled, True, True, 0)

        # ── Bottom action box (Reset button) ──
        act_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        act_box.set_margin_top(4)

        reset_btn = Gtk.Button.new_with_label("🔄 重置为默认模板")
        reset_btn.set_tooltip_text("恢复系统默认预置的润色提示词模板")
        def _on_reset_clicked(_btn):
            from stores.clipboard_store import _DEFAULT_POLISH_TEMPLATE
            b = self._polish_prompt_view.get_buffer()
            b.set_text(_DEFAULT_POLISH_TEMPLATE)
        reset_btn.connect("clicked", _on_reset_clicked)

        act_box.pack_start(reset_btn, False, False, 0)
        vbox.pack_start(act_box, False, False, 0)

        return self._make_tab_scrolled_window(vbox)

    # ── Tab: 流式输出 ────────────────────────────────────────────────────

    def _build_streaming_tab(self):
        """Build the streaming output v2/v3 settings tab page.

        Dropdown for streaming mode (off/text_only/full),
        checkbox for incremental tool cards (v3).
        """
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)

        # ── Streaming mode info ──
        mode_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        mode_lbl = Gtk.Label.new("流式模式:")
        mode_lbl.set_size_request(180, -1)
        mode_lbl.set_xalign(0)

        mode_val = Gtk.Label.new("完整 (full) — 流式文本 + 工具调用增量更新")
        mode_val.set_xalign(0)

        mode_hbox.pack_start(mode_lbl, False, False, 0)
        mode_hbox.pack_start(mode_val, False, False, 0)
        vbox.pack_start(mode_hbox, False, False, 0)

        mode_hint = Gtk.Label.new()
        mode_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "流式始终启用完整模式。旧版关闭/纯文本模式已移除。\n"
            "更改需重启应用后生效。"
            "</span>"
        )
        mode_hint.set_xalign(0)
        mode_hint.set_margin_top(8)
        vbox.pack_start(mode_hint, False, False, 0)

        # ── Separator ──
        sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(16)
        sep.set_margin_bottom(12)
        vbox.pack_start(sep, False, False, 0)

        # ── Incremental tools checkbox ──
        inc_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        inc_hbox.set_margin_top(8)
        self._incremental_tools_check = Gtk.CheckButton.new_with_label("启用增量工具卡片更新 (v3)")
        self._incremental_tools_check.set_active(self._ai_settings_store.enable_incremental_tools)
        inc_hbox.pack_start(self._incremental_tools_check, False, False, 0)
        vbox.pack_start(inc_hbox, False, False, 0)

        inc_hint = Gtk.Label.new()
        inc_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "每个工具结果返回时只更新对应卡片，不触发全量渲染。\n"
            "关闭后恢复旧版行为（每次工具结果都重新渲染整个对话轮次）。\n"
            "更改需重启应用。"
            "</span>"
        )
        inc_hint.set_xalign(0)
        inc_hint.set_margin_top(8)
        vbox.pack_start(inc_hint, False, False, 0)

        # ── Show tool details checkbox ──
        details_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        details_hbox.set_margin_top(16)
        self._show_tool_details_check = Gtk.CheckButton.new_with_label("显示工具调用结果详情")
        self._show_tool_details_check.set_active(self._ai_settings_store.show_tool_details)
        details_hbox.pack_start(self._show_tool_details_check, False, False, 0)
        vbox.pack_start(details_hbox, False, False, 0)

        details_hint = Gtk.Label.new()
        details_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "勾选时：工具卡片可展开查看完整返回结果。\n"
            "不勾选时：只显示工具名称和调用目的，不渲染结果内容，节省 CPU 和内存。\n"
            "更改需重启应用。"
            "</span>"
        )
        details_hint.set_xalign(0)
        details_hint.set_margin_top(8)
        vbox.pack_start(details_hint, False, False, 0)

        # ── Spacer ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return self._make_tab_scrolled_window(vbox)

    # ── Tab: 主题 ──────────────────────────────────────────────────────

    def _build_theme_tab(self):
        """Build the Theme configuration tab page.

        Allows switching between Dark and Light themes.
        The change is applied on Save via the ``on_theme_changed`` callback.
        """
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)

        # ── Dark / Light radio buttons ──
        theme_lbl = Gtk.Label.new()
        theme_lbl.set_markup("<b>界面主题</b>")
        theme_lbl.set_xalign(0)
        vbox.pack_start(theme_lbl, False, False, 0)

        self._theme_dark_radio = Gtk.RadioButton.new_with_label(None, "经典深色 (Dark)")
        self._theme_light_radio = Gtk.RadioButton.new_with_label_from_widget(
            self._theme_dark_radio, "浅色 (Light)"
        )
        self._theme_dark_moon_radio = Gtk.RadioButton.new_with_label_from_widget(
            self._theme_dark_radio, "紫月星云 (Dark Moon)"
        )
        if self._current_theme == "light":
            self._theme_light_radio.set_active(True)
        elif self._current_theme == "dark-moon":
            self._theme_dark_moon_radio.set_active(True)
        else:
            self._theme_dark_radio.set_active(True)

        vbox.pack_start(self._theme_dark_radio, False, False, 0)
        vbox.pack_start(self._theme_dark_moon_radio, False, False, 0)
        vbox.pack_start(self._theme_light_radio, False, False, 0)

        # ── Preview hint ──
        hint = Gtk.Label.new()
        theme_names = {"light": "Light (浅色)", "dark": "Dark (经典深色)", "dark-moon": "Dark Moon (紫月星云)"}
        theme_name = theme_names.get(self._current_theme, "Dark")
        hint.set_markup(
            f"<span size='small' foreground='#888888'>"
            f"当前主题：{theme_name}。\n"
            f"更改保存后立即生效。"
            f"</span>"
        )
        hint.set_xalign(0)
        hint.set_margin_top(12)
        vbox.pack_start(hint, False, False, 0)

        # ── Spacer ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return self._make_tab_scrolled_window(vbox)

    # ── Tab: 常量配置 ──────────────────────────────────────────────────

    def _build_constants_tab(self):
        """Build the constants configuration tab page.

        Contains user-configurable app-wide constants like clipboard max count.
        Add new rows here for future configurable constants.

        Returns a Gtk.ScrolledWindow ready for notebook.append_page().
        """
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)

        # ── Clipboard max history ──
        clip_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        clip_lbl = Gtk.Label.new("剪切板最大历史项目数:")
        clip_lbl.set_size_request(180, -1)
        clip_lbl.set_xalign(0)
        self._clip_max_spin = Gtk.SpinButton.new_with_range(10, 2000, 10)
        self._clip_max_spin.set_value(self._ai_settings_store.max_clipboard)
        clip_hbox.pack_start(clip_lbl, False, False, 0)
        clip_hbox.pack_start(self._clip_max_spin, False, False, 0)
        vbox.pack_start(clip_hbox, False, False, 0)

        # ── Hint ──
        hint = Gtk.Label.new()
        hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "超过此数量的旧剪切板历史将被自动丢弃。\n"
            "数值越大占用内存越多。更改需重启应用后生效。"
            "</span>"
        )
        hint.set_xalign(0)
        hint.set_margin_top(8)
        vbox.pack_start(hint, False, False, 0)

        # ── Separator before tool iterations ──
        sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(16)
        sep.set_margin_bottom(12)
        vbox.pack_start(sep, False, False, 0)

        # ── AI max ReAct iterations ──
        tool_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        tool_lbl = Gtk.Label.new("AI 最大迭代次数:")
        tool_lbl.set_size_request(180, -1)
        tool_lbl.set_xalign(0)
        self._tool_iter_spin = Gtk.SpinButton.new_with_range(5, 100, 5)
        self._tool_iter_spin.set_value(self._ai_settings_store.max_tool_iterations)
        tool_hbox.pack_start(tool_lbl, False, False, 0)
        tool_hbox.pack_start(self._tool_iter_spin, False, False, 0)
        vbox.pack_start(tool_hbox, False, False, 0)

        # ── Tool iteration hint ──
        tool_hint = Gtk.Label.new()
        tool_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "AI 单轮对话中 ReAct 循环的最大迭代次数。\n"
            "每次迭代 LLM 可能返回多个工具调用，因此实际工具调用数可能大于此值。\n"
            "次数越多可执行越复杂的多步任务，但消耗更多 token。\n"
            "更改需重启应用后生效。"
            "</span>"
        )
        tool_hint.set_xalign(0)
        tool_hint.set_margin_top(8)
        vbox.pack_start(tool_hint, False, False, 0)

        # ── Spacer ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return self._make_tab_scrolled_window(vbox)

    # ── Tab: MCP 服务器 (Master-Detail 左右分栏布局) ──────────────────────

    def _build_mcp_tab(self):
        """Build the MCP Server configuration tab page with a modern Master-Detail split layout.

        支持 stdio（本地子进程）和 http（远程 Streamable HTTP）两种模式。
        左侧为 MCP 服务器导航列表，右侧为当前选中的具体配置表单与测试面板。
        """
        main_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)

        # ── 左侧列表概览栏 (Master Sidebar) ──
        sidebar_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        sidebar_box.set_size_request(230, -1)
        sidebar_box.set_margin_start(14)
        sidebar_box.set_margin_end(8)
        sidebar_box.set_margin_top(12)
        sidebar_box.set_margin_bottom(12)

        # 顶部常驻「＋ 添加」按钮
        add_btn = Gtk.Button.new_with_label("＋ 添加 MCP 服务器")
        add_btn.set_tooltip_text("添加一个新的 MCP 服务器配置")
        add_btn.connect("clicked", lambda _: self._add_mcp_server_card(select_new=True))
        sidebar_box.pack_start(add_btn, False, False, 0)

        # 列表滚动视口
        list_scrolled = Gtk.ScrolledWindow.new()
        list_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scrolled.set_shadow_type(Gtk.ShadowType.IN)
        list_scrolled.set_vexpand(True)

        self._mcp_list_box = Gtk.ListBox.new()
        self._mcp_list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._mcp_list_box.connect("row-selected", self._on_mcp_row_selected)
        list_scrolled.add(self._mcp_list_box)
        sidebar_box.pack_start(list_scrolled, True, True, 0)

        # 底部状态计数信息
        self._mcp_count_label = Gtk.Label.new()
        self._mcp_count_label.set_xalign(0)
        self._mcp_count_label.set_markup("<span size='small' foreground='#888888'>共 0 个服务器</span>")
        sidebar_box.pack_start(self._mcp_count_label, False, False, 0)

        main_box.pack_start(sidebar_box, False, False, 0)

        # ── 垂直分割线 ──
        sep = Gtk.Separator.new(Gtk.Orientation.VERTICAL)
        main_box.pack_start(sep, False, False, 0)

        # ── 右侧详情编辑区 (Detail Stack) ──
        detail_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        detail_box.set_hexpand(True)
        detail_box.set_vexpand(True)
        detail_box.set_margin_start(12)
        detail_box.set_margin_end(16)
        detail_box.set_margin_top(12)
        detail_box.set_margin_bottom(12)

        self._mcp_stack = Gtk.Stack.new()
        self._mcp_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._mcp_stack.set_transition_duration(150)
        self._mcp_stack.set_hexpand(True)
        self._mcp_stack.set_vexpand(True)

        # 空状态占位页
        empty_view = self._create_mcp_empty_view()
        self._mcp_stack.add_named(empty_view, "empty")
        detail_box.pack_start(self._mcp_stack, True, True, 0)

        main_box.pack_start(detail_box, True, True, 0)

        # ── 加载已有服务器 ──
        self._mcp_server_widgets = []
        self._mcp_item_seq = 0
        existing_servers = getattr(self._ai_settings_store, "mcp_servers", [])
        for sd in existing_servers:
            self._add_mcp_server_card(sd, select_new=False)

        # 默认选中首项
        first_row = self._mcp_list_box.get_row_at_index(0)
        if first_row:
            self._mcp_list_box.select_row(first_row)
        else:
            self._mcp_stack.set_visible_child_name("empty")

        self._update_mcp_count_label()

        return main_box

    def _create_mcp_empty_view(self) -> Gtk.Widget:
        """创建无 MCP 服务器时的占位引导界面。"""
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 12)
        vbox.set_valign(Gtk.Align.CENTER)
        vbox.set_halign(Gtk.Align.CENTER)

        icon_label = Gtk.Label.new()
        icon_label.set_markup("<span size='xx-large' foreground='#888888'>🔌</span>")
        vbox.pack_start(icon_label, False, False, 0)

        title_label = Gtk.Label.new()
        title_label.set_markup("<span size='large' weight='bold'>暂无 MCP 服务器</span>")
        vbox.pack_start(title_label, False, False, 0)

        desc_label = Gtk.Label.new()
        desc_label.set_markup(
            "<span foreground='#888888' size='small'>"
            "MCP (Model Context Protocol) 允许 AI 助手连接本地工具或远程服务。\n"
            "点击左侧「<b>＋ 添加 MCP 服务器</b>」开始配置。"
            "</span>"
        )
        desc_label.set_justify(Gtk.Justification.CENTER)
        vbox.pack_start(desc_label, False, False, 0)

        return vbox

    def _update_mcp_count_label(self):
        """更新侧边栏底部服务器计数。"""
        count = len(self._mcp_server_widgets)
        enabled_count = sum(1 for w in self._mcp_server_widgets if w["enabled"].get_active())
        if hasattr(self, "_mcp_count_label"):
            self._mcp_count_label.set_markup(
                f"<span size='small' foreground='#888888'>共 {count} 个服务器 ({enabled_count} 已启用)</span>"
            )

    def _update_mcp_list_row(self, row: Gtk.ListBoxRow, name: str, enabled: bool, transport: str):
        """轻量级在位更新左侧列表行的状态指示灯、名称及传输徽标。"""
        if not row or not hasattr(row, "_dot_label"):
            return
        dot_color = "#4caf50" if enabled else "#888888"
        row._dot_label.set_markup(f"<span foreground='{dot_color}'>●</span>")
        display_name = name or "(未命名)"
        row._title_label.set_text(display_name)

        is_http = (transport == "http")
        badge_text = "http" if is_http else "stdio"
        badge_color = "#ab47bc" if is_http else "#00897b"
        badge_bg = "#f3e5f5" if is_http else "#e0f2f1"
        row._badge_label.set_markup(
            f"<span size='x-small' weight='bold' foreground='{badge_color}' background='{badge_bg}'> {badge_text} </span>"
        )

    def _on_mcp_row_selected(self, listbox, row):
        """左侧列表选中项变更时切换右侧表单页面。"""
        if row and hasattr(row, "_server_id"):
            self._mcp_stack.set_visible_child_name(row._server_id)
        else:
            if not self._mcp_server_widgets:
                self._mcp_stack.set_visible_child_name("empty")

    @staticmethod
    def _parse_env_str(env_raw: str) -> Dict[str, str]:
        """将 KEY=VAL, KEY2=VAL2 格式字符串解析为环境变量字典。"""
        env_dict = {}
        if env_raw:
            for item in env_raw.split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    if k.strip():
                        env_dict[k.strip()] = v.strip()
        return env_dict

    def _add_mcp_server_card(self, data: Optional[dict] = None, select_new: bool = False):
        """添加一个 MCP 服务器配置条目（左侧 ListBoxRow + 右侧 Stack 详情页）。"""
        cfg = MCPServerConfig.from_dict(data or {})
        server_id = f"mcp_server_{self._mcp_item_seq}"
        self._mcp_item_seq += 1

        # ── 1. 创建左侧列表行 (ListBoxRow) ──
        row = Gtk.ListBoxRow.new()
        row._server_id = server_id

        row_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        row_box.set_margin_start(6)
        row_box.set_margin_end(6)
        row_box.set_margin_top(6)
        row_box.set_margin_bottom(6)

        dot_label = Gtk.Label.new()
        row._dot_label = dot_label
        row_box.pack_start(dot_label, False, False, 0)

        title_label = Gtk.Label.new()
        title_label.set_xalign(0)
        title_label.set_hexpand(True)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        row._title_label = title_label
        row_box.pack_start(title_label, True, True, 0)

        badge_label = Gtk.Label.new()
        row._badge_label = badge_label
        row_box.pack_start(badge_label, False, False, 0)

        row.add(row_box)
        row.get_style_context().add_class("mcp-sidebar-row")
        self._update_mcp_list_row(row, cfg.name, cfg.enabled, cfg.transport)
        self._mcp_list_box.add(row)
        row.show_all()

        # ── 2. 创建右侧表单详情页 (Detail Pane in ScrolledWindow) ──
        detail_scrolled = Gtk.ScrolledWindow.new()
        detail_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        detail_scrolled.set_shadow_type(Gtk.ShadowType.NONE)

        detail_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 10)
        detail_vbox.set_margin_start(4)
        detail_vbox.set_margin_end(4)
        detail_vbox.set_margin_top(4)
        detail_vbox.set_margin_bottom(12)
        detail_scrolled.add(detail_vbox)

        # ── 区域 1: 顶部控制卡片 (Name + Enable + Auto-connect + Delete) ──
        header_frame = Gtk.Frame.new()
        header_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        header_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        header_vbox.set_margin_start(10)
        header_vbox.set_margin_end(10)
        header_vbox.set_margin_top(8)
        header_vbox.set_margin_bottom(8)
        header_frame.add(header_vbox)

        row1 = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)

        name_lbl = Gtk.Label.new("名称:")
        name_lbl.set_xalign(0)
        row1.pack_start(name_lbl, False, False, 0)

        name_entry = Gtk.Entry.new()
        name_entry.set_placeholder_text("例如: filesystem 或 github")
        name_entry.set_hexpand(True)
        name_entry.set_text(cfg.name)
        row1.pack_start(name_entry, True, True, 0)

        enabled_check = Gtk.CheckButton.new_with_label("启用")
        enabled_check.set_active(cfg.enabled)
        row1.pack_start(enabled_check, False, False, 0)

        auto_check = Gtk.CheckButton.new_with_label("自动连接")
        auto_check.set_active(cfg.auto_connect)
        row1.pack_start(auto_check, False, False, 0)

        del_btn = Gtk.Button.new_with_label("🗑 删除")
        del_btn.set_tooltip_text("删除此 MCP 服务器")
        del_btn.connect("clicked", lambda _: self._remove_mcp_server_card(server_id))
        row1.pack_start(del_btn, False, False, 0)

        header_vbox.pack_start(row1, False, False, 0)

        # 传输方式选择行
        row_trans = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        trans_lbl = Gtk.Label.new("传输方式:")
        trans_lbl.set_xalign(0)
        row_trans.pack_start(trans_lbl, False, False, 0)

        transport_combo = Gtk.ComboBoxText.new()
        transport_combo.append("stdio", "stdio（本地命令行子进程）")
        transport_combo.append("http", "http（远程 Streamable HTTP 服务）")
        transport_combo.set_active_id(cfg.transport)
        transport_combo.set_hexpand(True)
        row_trans.pack_start(transport_combo, True, True, 0)

        header_vbox.pack_start(row_trans, False, False, 0)
        detail_vbox.pack_start(header_frame, False, False, 0)

        # ── 区域 2: 传输参数卡片 (Stdio vs HTTP) ──
        param_frame = Gtk.Frame.new()
        param_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        param_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        param_vbox.set_margin_start(10)
        param_vbox.set_margin_end(10)
        param_vbox.set_margin_top(8)
        param_vbox.set_margin_bottom(8)
        param_frame.add(param_vbox)

        # Stdio 容器
        stdio_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)

        row2_stdio = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        cmd_entry = Gtk.Entry.new()
        cmd_entry.set_placeholder_text("执行命令（如 npx 或 python3）")
        cmd_entry.set_text(cfg.command)
        cmd_entry.set_size_request(180, -1)
        row2_stdio.pack_start(cmd_entry, False, False, 0)

        args_entry = Gtk.Entry.new()
        args_entry.set_placeholder_text("命令参数（空格分隔，如 -y @modelcontextprotocol/server-filesystem /tmp）")
        args_entry.set_hexpand(True)
        args_entry.set_text(" ".join(cfg.args))
        row2_stdio.pack_start(args_entry, True, True, 0)
        stdio_box.pack_start(row2_stdio, False, False, 0)

        row2_5_stdio = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        cwd_entry = Gtk.Entry.new()
        cwd_entry.set_placeholder_text("工作目录 cwd（可选，如 /home/user/project）")
        cwd_entry.set_text(cfg.cwd or "")
        cwd_entry.set_hexpand(True)
        row2_5_stdio.pack_start(cwd_entry, True, True, 0)

        env_entry = Gtk.Entry.new()
        env_entry.set_placeholder_text("环境变量（可选，KEY=VAL, KEY2=VAL2）")
        env_entry.set_hexpand(True)
        env_text = ", ".join(f"{k}={v}" for k, v in (cfg.env or {}).items())
        env_entry.set_text(env_text)
        row2_5_stdio.pack_start(env_entry, True, True, 0)
        stdio_box.pack_start(row2_5_stdio, False, False, 0)

        param_vbox.pack_start(stdio_box, False, False, 0)

        # HTTP 容器
        http_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)

        row3_http = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        url_entry = Gtk.Entry.new()
        url_entry.set_placeholder_text("URL 端点（如 https://api.githubcopilot.com/mcp/）")
        url_entry.set_hexpand(True)
        url_entry.set_text(cfg.url)
        row3_http.pack_start(url_entry, True, True, 0)

        auth_combo = Gtk.ComboBoxText.new()
        auth_combo.append("none", "无认证")
        auth_combo.append("bearer", "Bearer Token")
        auth_combo.append("oauth2", "OAuth 2.1")
        auth_combo.set_active_id(cfg.auth_type or "bearer")
        auth_combo.set_tooltip_text("HTTP 认证方式")
        row3_http.pack_start(auth_combo, False, False, 0)

        api_key_entry = Gtk.Entry.new()
        api_key_entry.set_placeholder_text("API Key / Bearer Token")
        api_key_entry.set_text(cfg.api_key)
        api_key_entry.set_width_chars(25)
        api_key_entry.set_visibility(False)
        row3_http.pack_start(api_key_entry, False, False, 0)
        http_box.pack_start(row3_http, False, False, 0)

        row4_http_advanced = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        proto_combo = Gtk.ComboBoxText.new()
        proto_combo.append("2025-11-25", "2025-11-25（稳定标准）")
        proto_combo.append("2026-07-28", "2026-07-28（无状态新版）")
        proto_combo.set_active_id(cfg.protocol_version or "2025-11-25")
        proto_combo.set_tooltip_text("MCP 协议版本")
        row4_http_advanced.pack_start(proto_combo, False, False, 0)

        header_check = Gtk.CheckButton.new_with_label("发送 2026 新 Headers")
        header_check.set_active(cfg.enable_2026_headers)
        header_check.set_tooltip_text("启用 Mcp-Method / Mcp-Name 等 2026-07-28 请求头")
        row4_http_advanced.pack_start(header_check, False, False, 0)
        http_box.pack_start(row4_http_advanced, False, False, 0)

        # OAuth 2.1 配置行
        row5_oauth = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        oauth_client_id_entry = Gtk.Entry.new()
        oauth_client_id_entry.set_placeholder_text("Client ID（可选）")
        oauth_client_id_entry.set_text(cfg.oauth_client_id)
        oauth_client_id_entry.set_width_chars(14)
        row5_oauth.pack_start(oauth_client_id_entry, False, False, 0)

        oauth_client_secret_entry = Gtk.Entry.new()
        oauth_client_secret_entry.set_placeholder_text("Client Secret（可选）")
        oauth_client_secret_entry.set_text(cfg.oauth_client_secret)
        oauth_client_secret_entry.set_width_chars(14)
        oauth_client_secret_entry.set_visibility(False)
        row5_oauth.pack_start(oauth_client_secret_entry, False, False, 0)

        oauth_token_url_entry = Gtk.Entry.new()
        oauth_token_url_entry.set_placeholder_text("Token URL（可选）")
        oauth_token_url_entry.set_text(cfg.oauth_token_url)
        oauth_token_url_entry.set_width_chars(18)
        row5_oauth.pack_start(oauth_token_url_entry, False, False, 0)

        oauth_scopes_entry = Gtk.Entry.new()
        oauth_scopes_entry.set_placeholder_text("Scopes（可选）")
        oauth_scopes_entry.set_text(cfg.oauth_scopes)
        oauth_scopes_entry.set_width_chars(14)
        row5_oauth.pack_start(oauth_scopes_entry, False, False, 0)

        oauth_auth_btn = Gtk.Button.new_with_label("开始授权")
        oauth_auth_btn.set_tooltip_text("在浏览器中完成 OAuth 授权（PKCE）")
        row5_oauth.pack_start(oauth_auth_btn, False, False, 0)

        oauth_clear_btn = Gtk.Button.new_with_label("清除授权")
        oauth_clear_btn.set_tooltip_text("删除本地保存的 OAuth Token")
        row5_oauth.pack_start(oauth_clear_btn, False, False, 0)

        oauth_status_label = Gtk.Label.new()
        oauth_status_label.set_markup("<span foreground='#888'>未授权</span>")
        row5_oauth.pack_start(oauth_status_label, False, False, 0)
        http_box.pack_start(row5_oauth, False, False, 0)

        param_vbox.pack_start(http_box, False, False, 0)
        detail_vbox.pack_start(param_frame, False, False, 0)

        # ── 区域 3: 连接测试与状态卡片 ──
        test_frame = Gtk.Frame.new()
        test_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        test_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 10)
        test_box.set_margin_start(10)
        test_box.set_margin_end(10)
        test_box.set_margin_top(8)
        test_box.set_margin_bottom(8)
        test_frame.add(test_box)

        test_btn = Gtk.Button.new_with_label("⚡ 测试连接")
        test_btn.set_tooltip_text("发送 initialize 请求验证连通性与获取工具列表")
        test_box.pack_start(test_btn, False, False, 0)

        status_label = Gtk.Label.new()
        last_status = data.get("last_status", "") if data else ""
        if last_status == "connected":
            status_label.set_markup("<span foreground='green'>● 已连接</span>")
        elif last_status and last_status.startswith("error"):
            status_label.set_markup(f"<span foreground='red'>● {last_status}</span>")
        else:
            status_label.set_markup("<span foreground='#888'>○ 未连接</span>")
        test_box.pack_start(status_label, False, False, 0)

        detail_vbox.pack_start(test_frame, False, False, 0)

        # ── 3. 注册到 Stack 与组件引用列表 ──
        stdio_box.set_no_show_all(True)
        http_box.set_no_show_all(True)
        api_key_entry.set_no_show_all(True)
        row5_oauth.set_no_show_all(True)

        self._mcp_stack.add_named(detail_scrolled, server_id)
        detail_scrolled.show_all()

        widget_item = {
            "server_id": server_id,
            "row": row,
            "page": detail_scrolled,
            "name": name_entry,
            "transport": transport_combo,
            "command": cmd_entry,
            "args": args_entry,
            "cwd": cwd_entry,
            "env": env_entry,
            "url": url_entry,
            "api_key": api_key_entry,
            "auth_type": auth_combo,
            "oauth_client_id": oauth_client_id_entry,
            "oauth_client_secret": oauth_client_secret_entry,
            "oauth_token_url": oauth_token_url_entry,
            "oauth_scopes": oauth_scopes_entry,
            "oauth_auth_btn": oauth_auth_btn,
            "oauth_clear_btn": oauth_clear_btn,
            "oauth_status_label": oauth_status_label,
            "protocol_version": proto_combo,
            "enable_2026_headers": header_check,
            "enabled": enabled_check,
            "auto_connect": auto_check,
            "status_label": status_label,
            "box": detail_scrolled,
        }
        self._mcp_server_widgets.append(widget_item)

        # ── 4. 显隐控制 ──
        def _apply_visibility(is_http_mode: bool):
            stdio_box.set_visible(not is_http_mode)
            http_box.set_visible(is_http_mode)
            mode = auth_combo.get_active_id()
            api_key_entry.set_visible(is_http_mode and mode == "bearer")
            row5_oauth.set_visible(is_http_mode and mode == "oauth2")

        is_http = (cfg.transport == "http")
        _apply_visibility(is_http)

        detail_scrolled.connect("map", lambda *_: _apply_visibility(
            transport_combo.get_active_id() == "http"
        ))

        # ── 5. 交互事件与双向联动 ──
        name_entry.connect("changed", lambda e: self._update_mcp_list_row(
            row, e.get_text().strip(), enabled_check.get_active(), transport_combo.get_active_id()
        ))
        enabled_check.connect("toggled", lambda s: (
            self._update_mcp_list_row(row, name_entry.get_text().strip(), s.get_active(), transport_combo.get_active_id()),
            self._update_mcp_count_label(),
        ))
        transport_combo.connect("changed", lambda cb: (
            _apply_visibility(cb.get_active_id() == "http"),
            self._update_mcp_list_row(row, name_entry.get_text().strip(), enabled_check.get_active(), cb.get_active_id()),
        ))

        # OAuth 状态与事件
        def _update_oauth_status():
            try:
                from mcp_integration.oauth.discovery import canonical_server_uri
                from mcp_integration.oauth.token_store import OAuthTokenStore
                url = url_entry.get_text().strip()
                if not url:
                    oauth_status_label.set_markup("<span foreground='#888'>未授权</span>")
                    return
                status = OAuthTokenStore(canonical_server_uri(url)).get_status()
                if status == "已授权":
                    oauth_status_label.set_markup("<span foreground='green'>● 已授权</span>")
                elif status == "已过期":
                    oauth_status_label.set_markup("<span foreground='orange'>● 已过期</span>")
                else:
                    oauth_status_label.set_markup("<span foreground='#888'>未授权</span>")
            except Exception:
                oauth_status_label.set_markup("<span foreground='#888'>未授权</span>")

        def _on_auth_changed(combo):
            is_http_mode = (transport_combo.get_active_id() == "http")
            mode = combo.get_active_id()
            api_key_entry.set_visible(is_http_mode and mode == "bearer")
            row5_oauth.set_visible(is_http_mode and mode == "oauth2")
            if is_http_mode and mode == "oauth2":
                _update_oauth_status()

        auth_combo.connect("changed", _on_auth_changed)
        _on_auth_changed(auth_combo)

        # 开始授权
        def _on_start_oauth_clicked(_btn):
            name = name_entry.get_text().strip()
            url = url_entry.get_text().strip()
            if not name or not url:
                oauth_status_label.set_markup("<span foreground='red'>● 需先填写名称和 URL</span>")
                return
            oauth_status_label.set_markup("<span foreground='orange'>● 授权中…请在浏览器完成</span>")

            def _do_auth():
                import asyncio
                import threading
                try:
                    from mcp_integration.oauth.provider import OAuth2AuthProvider
                    provider = OAuth2AuthProvider(
                        url,
                        client_id=oauth_client_id_entry.get_text().strip(),
                        client_secret=oauth_client_secret_entry.get_text().strip(),
                        token_url=oauth_token_url_entry.get_text().strip(),
                        scopes=oauth_scopes_entry.get_text().strip(),
                    )
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(provider.get_auth_headers())
                    finally:
                        loop.close()
                    GLib.idle_add(lambda: oauth_status_label.set_markup(
                        "<span foreground='green'>● 已授权 ✓</span>"
                    ))
                except Exception as e:
                    err_msg = str(e)
                    GLib.idle_add(lambda msg=err_msg: oauth_status_label.set_markup(
                        f"<span foreground='red'>● 授权失败: {msg}</span>"
                    ))

            import threading
            threading.Thread(target=_do_auth, daemon=True).start()

        oauth_auth_btn.connect("clicked", _on_start_oauth_clicked)

        # 清除授权
        def _on_clear_oauth_clicked(_btn):
            url = url_entry.get_text().strip()
            if not url:
                oauth_status_label.set_markup("<span foreground='#888'>未授权</span>")
                return
            try:
                from mcp_integration.oauth.discovery import canonical_server_uri
                from mcp_integration.oauth.token_store import OAuthTokenStore
                OAuthTokenStore(canonical_server_uri(url)).clear()
                oauth_status_label.set_markup("<span foreground='#888'>已清除授权</span>")
            except Exception as e:
                oauth_status_label.set_markup(f"<span foreground='red'>● 清除失败: {e}</span>")

        oauth_clear_btn.connect("clicked", _on_clear_oauth_clicked)

        # 测试连接
        test_btn.connect("clicked", lambda _: self._on_test_mcp_connection(
            name_entry, transport_combo,
            cmd_entry, args_entry,
            url_entry, api_key_entry, auth_combo,
            status_label,
        ))

        # 选中新添加项
        if select_new:
            self._mcp_list_box.select_row(row)
            self._mcp_stack.set_visible_child_name(server_id)
            name_entry.grab_focus()

        self._update_mcp_count_label()

    def _on_test_mcp_connection(
        self,
        name_entry,
        transport_combo,
        cmd_entry,
        args_entry,
        url_entry,
        api_key_entry,
        auth_combo,
        status_label,
    ):
        """测试 MCP Server 连接。启动真实的 initialize 握手验证连通性。"""
        name = name_entry.get_text().strip()
        transport = transport_combo.get_active_id()
        cmd = cmd_entry.get_text().strip()
        args_text = args_entry.get_text().strip()
        args_list = args_text.split() if args_text else []
        url = url_entry.get_text().strip()
        api_key = api_key_entry.get_text().strip()
        auth_type = auth_combo.get_active_id()

        # 读取卡片配置（通过卡片 widgets 中保存的 entry）
        card_cfg = next(
            (w for w in self._mcp_server_widgets if w["name"] is name_entry),
            None,
        )
        cwd_val = None
        env_dict = {}
        oauth_client_id = ""
        oauth_client_secret = ""
        oauth_token_url = ""
        oauth_scopes = ""
        if card_cfg is not None:
            if card_cfg.get("cwd"):
                cwd_val = card_cfg["cwd"].get_text().strip() or None
            if card_cfg.get("env"):
                env_dict = self._parse_env_str(card_cfg["env"].get_text().strip())
            oauth_client_id = card_cfg["oauth_client_id"].get_text().strip() if card_cfg.get("oauth_client_id") else ""
            oauth_client_secret = card_cfg["oauth_client_secret"].get_text().strip() if card_cfg.get("oauth_client_secret") else ""
            oauth_token_url = card_cfg["oauth_token_url"].get_text().strip() if card_cfg.get("oauth_token_url") else ""
            oauth_scopes = card_cfg["oauth_scopes"].get_text().strip() if card_cfg.get("oauth_scopes") else ""

        if not name:
            status_label.set_markup("<span foreground='red'>● 名称不能为空</span>")
            return

        if transport == "http":
            if not url:
                status_label.set_markup("<span foreground='red'>● URL 不能为空</span>")
                return
        else:  # stdio
            if not cmd:
                status_label.set_markup("<span foreground='red'>● 命令不能为空</span>")
                return

        status_label.set_markup("<span foreground='orange'>● 测试中…</span>")

        import threading
        def _do_test():
            import asyncio
            try:
                config = MCPServerConfig(
                    name=name,
                    transport=transport,
                    command=cmd,
                    args=args_list,
                    cwd=cwd_val,
                    env=env_dict,
                    url=url,
                    api_key=api_key,
                    auth_type=auth_type,
                    oauth_client_id=oauth_client_id,
                    oauth_client_secret=oauth_client_secret,
                    oauth_token_url=oauth_token_url,
                    oauth_scopes=oauth_scopes,
                    auto_connect=False,
                )

                mgr = MCPClientManager()
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    ok, msg = loop.run_until_complete(mgr.connect_by_config(config))
                    if ok:
                        loop.run_until_complete(mgr.disconnect(name))
                        GLib.idle_add(
                            lambda: status_label.set_markup(
                                "<span foreground='green'>● 测试通过 ✓</span>"
                            )
                        )
                    else:
                        GLib.idle_add(
                            lambda: status_label.set_markup(
                                f"<span foreground='red'>● {msg}</span>"
                            )
                        )
                finally:
                    loop.close()
            except Exception as e:
                err_msg = str(e)
                GLib.idle_add(
                    lambda msg=err_msg: status_label.set_markup(
                        f"<span foreground='red'>● 测试异常: {msg}</span>"
                    )
                )

        threading.Thread(target=_do_test, daemon=True).start()

    def _remove_mcp_server_card(self, target):
        """移除一个 MCP 服务器配置（支持传入 server_id 或 widget box/frame）。"""
        item = None
        if isinstance(target, str):
            item = next((w for w in self._mcp_server_widgets if w.get("server_id") == target), None)
        else:
            item = next((w for w in self._mcp_server_widgets if w.get("box") is target or w.get("page") is target), None)

        if not item:
            return

        server_id = item.get("server_id")
        row = item.get("row")
        page = item.get("page")

        # 确定删除后应选中的下一个行
        next_select_row = None
        if row:
            idx = row.get_index()
            children = self._mcp_list_box.get_children()
            if len(children) > 1:
                if idx + 1 < len(children):
                    next_select_row = children[idx + 1]
                elif idx - 1 >= 0:
                    next_select_row = children[idx - 1]

        self._mcp_server_widgets = [w for w in self._mcp_server_widgets if w.get("server_id") != server_id]

        if row and row.get_parent() == self._mcp_list_box:
            self._mcp_list_box.remove(row)
        if page and page.get_parent() == self._mcp_stack:
            self._mcp_stack.remove(page)

        self._update_mcp_count_label()

        if next_select_row:
            self._mcp_list_box.select_row(next_select_row)
        else:
            first = self._mcp_list_box.get_row_at_index(0)
            if first:
                self._mcp_list_box.select_row(first)
            else:
                self._mcp_stack.set_visible_child_name("empty")

    # ── Save logic ──────────────────────────────────────────────────────

    def _on_save(self):
        """Persist all settings and close the dialog."""
        # QQ Mail credentials
        self._qq_store.email = self._email_entry.get_text().strip()
        self._qq_store.auth_code = self._auth_entry.get_text().strip()
        self._qq_store.max_body_chars = int(self._body_chars_spin.get_value())
        self._qq_store.save()

        # AI 对话设置
        self._ai_settings_store.soft_limit = int(self._soft_spin.get_value())
        self._ai_settings_store.trim_target = int(self._trim_spin.get_value())
        self._ai_settings_store.enable_summary = self._enable_summary_check.get_active()
        self._ai_settings_store.summary_threshold = int(self._summary_thresh_spin.get_value())
        self._ai_settings_store.summary_max_chars = int(self._summary_max_spin.get_value())
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
        self._ai_settings_store.max_clipboard = int(self._clip_max_spin.get_value())
        self._ai_settings_store.max_tool_iterations = int(self._tool_iter_spin.get_value())
        # 流式输出设置（始终为 full 模式，仅保留增量工具和详情选项）
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
        for w in self._tool_toggle_widgets:
            if not w["check"].get_active():
                disabled.append(w["name"])
        self._ai_settings_store.disabled_tools = disabled

        # MCP 服务器配置
        mcp_servers = []
        for w in self._mcp_server_widgets:
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
