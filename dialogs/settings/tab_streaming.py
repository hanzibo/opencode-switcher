"""Streaming configuration tab."""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


class StreamingTabMixin:
    """流式输出设置标签页。"""

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
