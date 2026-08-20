"""Constants configuration tab (clipboard max items and max tool iterations)."""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


class ConstantsTabMixin:
    """常量配置设置标签页。"""

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
