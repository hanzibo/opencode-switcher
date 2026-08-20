"""Prompt templates configuration tabs (System Prompt & Polish Prompt)."""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


class PromptsTabMixin:
    """系统提示词与润色提示词标签页。"""

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
