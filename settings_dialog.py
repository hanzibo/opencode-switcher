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
from ai_text_utils import set_code_highlight


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
            ("流式输出", self._build_streaming_tab),
            ("常量配置", self._build_constants_tab),
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

        # ── Spacer ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return outer_sw

    # ── Tab: 流式输出 ────────────────────────────────────────────────────

    def _build_streaming_tab(self):
        """Build the streaming output v2/v3 settings tab page.

        Dropdown for streaming mode (off/text_only/full),
        checkbox for incremental tool cards (v3).
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

        # ── Streaming mode dropdown ──
        mode_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        mode_lbl = Gtk.Label.new("流式 v2 模式:")
        mode_lbl.set_size_request(180, -1)
        mode_lbl.set_xalign(0)

        self._streaming_mode_combo = Gtk.ComboBoxText.new()
        self._streaming_mode_combo.append("off", "关闭 (off)")
        self._streaming_mode_combo.append("text_only", "纯文本 (text_only)")
        self._streaming_mode_combo.append("full", "完整 (full)")
        # Set active from store
        current_mode = self._ai_settings_store.streaming_v2_mode
        if current_mode == "off":
            self._streaming_mode_combo.set_active(0)
        elif current_mode == "text_only":
            self._streaming_mode_combo.set_active(1)
        else:
            self._streaming_mode_combo.set_active(2)  # full (default)

        mode_hbox.pack_start(mode_lbl, False, False, 0)
        mode_hbox.pack_start(self._streaming_mode_combo, False, False, 0)
        vbox.pack_start(mode_hbox, False, False, 0)

        mode_hint = Gtk.Label.new()
        mode_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "• off    — 关闭流式 v2，使用旧版全量渲染\n"
            "• text_only — v2 仅纯文本流式，工具调用阶段回退全量渲染\n"
            "• full   — v2 完整模式：流式文本 + 工具调用增量更新\n"
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
            "需在「流式 v2 模式」为 full 时生效。更改需重启应用。"
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

        return outer_sw

    # ── Tab: 常量配置 ──────────────────────────────────────────────────

    def _build_constants_tab(self):
        """Build the constants configuration tab page.

        Contains user-configurable app-wide constants like clipboard max count.
        Add new rows here for future configurable constants.

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
        buf = self._summary_prompt_view.get_buffer()
        self._ai_settings_store.summary_prompt_template = buf.get_text(
            buf.get_start_iter(), buf.get_end_iter(), False
        )
        self._ai_settings_store.max_clipboard = int(self._clip_max_spin.get_value())
        self._ai_settings_store.max_tool_iterations = int(self._tool_iter_spin.get_value())
        # 流式输出设置
        streaming_id = self._streaming_mode_combo.get_active_id()
        if streaming_id:
            self._ai_settings_store.streaming_v2_mode = streaming_id
        self._ai_settings_store.enable_incremental_tools = self._incremental_tools_check.get_active()
        self._ai_settings_store.show_tool_details = self._show_tool_details_check.get_active()
        self._ai_settings_store.enable_code_highlight = self._code_highlight_check.get_active()
        set_code_highlight(self._ai_settings_store.enable_code_highlight)
        self._ai_settings_store.save()

        if self._dialog:
            self._dialog.destroy()
