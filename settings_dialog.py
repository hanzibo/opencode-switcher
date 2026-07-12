"""Settings dialog — tabbed configuration window.

Extensible Gtk.Notebook-based settings dialog.  Start with a QQ Mail
credentials tab, add more tabs by appending to the _tabs registry.

Pattern references:
  - sort_cats_dialog.py      → Gtk.Notebook usage
  - prompts_config_dialog.py → API-key visibility toggle
  - sort_cats_dialog.py      → custom-dialog + focus guards
"""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk
from typing import Optional, Callable

from clipboard_store import QQMailCredentialsStore, AISettingsStore


def show_settings_dialog(parent_window: Gtk.Window,
                         ai_settings_store: Optional[AISettingsStore] = None,
                         on_dialog_shown: Optional[Callable[[], None]] = None,
                         on_dialog_hidden: Optional[Callable[[], None]] = None):
    """Factory: create and show the Settings dialog."""
    SettingsDialog(parent_window, ai_settings_store, on_dialog_shown, on_dialog_hidden)


class SettingsDialog:
    """Tabbed settings window.

    Tabs are defined in self._tabs as (name, builder_method) pairs.
    Add a new tab by appending to the list — the Notebook is built
    iteratively in build_ui().
    """

    def __init__(self, parent_window: Gtk.Window,
                 ai_settings_store: Optional[AISettingsStore] = None,
                 on_dialog_shown: Optional[Callable[[], None]] = None,
                 on_dialog_hidden: Optional[Callable[[], None]] = None):
        self.parent_window = parent_window
        self.on_dialog_shown = on_dialog_shown
        self.on_dialog_hidden = on_dialog_hidden

        # ── Tab registry: extend here for future tabs ──
        self._tabs = [
            ("QQ邮箱", self._build_qq_mail_tab),
            ("AI 对话", self._build_ai_settings_tab),
        ]

        self._qq_store = QQMailCredentialsStore()
        self._ai_settings_store = ai_settings_store or AISettingsStore()
        self._dialog = None
        self.build_ui()

    # ── UI Construction ──────────────────────────────────────────────────

    def build_ui(self):
        dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        dialog.get_style_context().add_class("custom-dialog")
        dialog.set_title("Settings")
        dialog.set_modal(True)
        dialog.set_default_size(600, 400)
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

    # ── Tab: QQ Mail ─────────────────────────────────────────────────────

    def _build_qq_mail_tab(self):
        """Build the QQ Mail credentials tab page.

        Returns a Gtk.ScrolledWindow ready for notebook.append_page().
        """
        outer_sw = Gtk.ScrolledWindow.new()
        outer_sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer_sw.set_vexpand(True)

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        outer_sw.add(vbox)

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

        return outer_sw

    # ── Tab: AI 对话 ───────────────────────────────────────────────────

    def _build_ai_settings_tab(self):
        """Build the AI conversation truncation settings tab page.

        Returns a Gtk.ScrolledWindow ready for notebook.append_page().
        """
        outer_sw = Gtk.ScrolledWindow.new()
        outer_sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer_sw.set_vexpand(True)

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        outer_sw.add(vbox)

        # ── Soft limit (triggering threshold) ──
        soft_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        soft_lbl = Gtk.Label.new("触发截断的消息数:")
        soft_lbl.set_size_request(150, -1)
        soft_lbl.set_xalign(0)
        self._soft_spin = Gtk.SpinButton.new_with_range(50, 500, 10)
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

        # ── Spacer ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return outer_sw

    # ── Save logic ──────────────────────────────────────────────────────

    def _on_save(self):
        """Persist all settings and close the dialog."""
        # QQ Mail credentials
        self._qq_store.email = self._email_entry.get_text().strip()
        self._qq_store.auth_code = self._auth_entry.get_text().strip()
        self._qq_store.save()

        # AI 对话设置
        self._ai_settings_store.soft_limit = int(self._soft_spin.get_value())
        self._ai_settings_store.trim_target = int(self._trim_spin.get_value())
        self._ai_settings_store.enable_summary = self._enable_summary_check.get_active()
        self._ai_settings_store.summary_threshold = int(self._summary_thresh_spin.get_value())
        self._ai_settings_store.summary_max_chars = int(self._summary_max_spin.get_value())
        self._ai_settings_store.save()

        if self._dialog:
            self._dialog.destroy()
