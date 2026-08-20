"""Mail configuration tabs (QQ Mail credentials & Gmail OAuth 2.0)."""

import os
import threading
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib


class MailTabMixin:
    """QQ 邮箱与 Gmail 授权设置标签页。"""

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
