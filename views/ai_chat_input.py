"""输入框/附件/斜杠命令 — AI 聊天输入区域。

职责：
- 多行 Gtk.TextView 输入框
- 图片/文件拖拽与剪贴板图片粘贴
- 附件预览栏
- 斜杠指令解析与弹窗补全
- 发送/暂停按钮控制
"""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("PangoCairo", "1.0")

import re
import html
import json
import hashlib
import mimetypes
import urllib.parse
import os
import threading
from urllib.parse import urlparse
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Pango, PangoCairo
from typing import Optional, Callable, List, Dict, Tuple, Any

from stores.clipboard_store import CONFIG_DIR, CustomPrompt
from ai_text_utils import (
    _image_to_data_uri,
    _vision_content_to_text,
    _markdown_to_html_safe,
)
from views.ai_popovers import AICommandPopover

AI_BTN_LABEL_SEND = "发送"
AI_BTN_LABEL_STOP = "暂停"


class AIChatInputArea(Gtk.Box):
    """AI 聊天输入区域：输入框、附件栏、发送按钮与斜杠命令。"""

    # Slash commands available in the AI chat input box (command, description)
    _AI_COMMANDS = [
        ("/new", "新对话"),
        ("/delete", "删除并新建"),
        ("/retry", "回滚到上一轮"),
        ("/rollback", "回滚到任意轮"),
        ("/title", "设置/生成标题"),
        ("/model", "切换模型"),
        ("/cd", "切换 bash 工作路径"),
        ("/summary", "压缩上下文（/summary keep=N，保留最近N条，默认50）"),
        ("/skill", "查看与手动触发 AI Skill"),
    ]

    def __init__(
        self,
        on_send_clicked_cb=None,
        on_new_conversation_cb=None,
        on_attach_cb=None,
        on_title_command_cb=None,
        on_summary_command_cb=None,
        on_rollback_command_cb=None,
        on_retry_command_cb=None,
        on_model_command_cb=None,
        on_cd_command_cb=None,
        on_skill_command_cb=None,
        on_delete_command_cb=None,
        on_entry_changed_cb=None,
        get_streaming_state_fn=None,
        get_cancelling_state_fn=None,
        get_pending_image_fn=None,
        get_selected_subagents_fn=None,
        get_ai_messages_fn=None,
        get_llm_settings_store_fn=None,
        get_active_model_info_fn=None,
        get_ai_conversation_id_fn=None,
        get_conversation_store_fn=None,
        on_copy_started_cb=None,
        on_copy_finished_cb=None,
        get_history_queries_fn=None,
        set_history_queries_fn=None,
        get_history_index_fn=None,
        set_history_index_fn=None,
        get_current_draft_fn=None,
        set_current_draft_fn=None,
        on_dialog_shown_cb=None,
        on_dialog_hidden_cb=None,
        on_menu_shown_cb=None,
        on_menu_hidden_cb=None,
        append_html_cb=None,
        get_ai_markdown_text_fn=None,
        set_entry_placeholder_cb=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.set_no_show_all(True)
        self.set_margin_top(4)

        # Callbacks
        self._on_send_clicked_cb = on_send_clicked_cb
        self._on_new_conversation_cb = on_new_conversation_cb
        self._on_attach_cb = on_attach_cb
        self._on_title_command_cb = on_title_command_cb
        self._on_summary_command_cb = on_summary_command_cb
        self._on_rollback_command_cb = on_rollback_command_cb
        self._on_retry_command_cb = on_retry_command_cb
        self._on_model_command_cb = on_model_command_cb
        self._on_cd_command_cb = on_cd_command_cb
        self._on_skill_command_cb = on_skill_command_cb
        self._on_delete_command_cb = on_delete_command_cb
        self._on_entry_changed_cb = on_entry_changed_cb
        self._on_copy_started_cb = on_copy_started_cb
        self._on_copy_finished_cb = on_copy_finished_cb
        self._on_dialog_shown_cb = on_dialog_shown_cb
        self._on_dialog_hidden_cb = on_dialog_hidden_cb
        self._on_menu_shown_cb = on_menu_shown_cb
        self._on_menu_hidden_cb = on_menu_hidden_cb
        self._append_html_cb = append_html_cb
        self._set_entry_placeholder_cb = set_entry_placeholder_cb

        # State accessors
        self._get_streaming_state_fn = get_streaming_state_fn
        self._get_cancelling_state_fn = get_cancelling_state_fn
        self._get_pending_image_fn = get_pending_image_fn
        self._get_selected_subagents_fn = get_selected_subagents_fn
        self._get_ai_messages_fn = get_ai_messages_fn
        self._get_llm_settings_store_fn = get_llm_settings_store_fn
        self._get_active_model_info_fn = get_active_model_info_fn
        self._get_ai_conversation_id_fn = get_ai_conversation_id_fn
        self._get_conversation_store_fn = get_conversation_store_fn
        self._get_history_queries_fn = get_history_queries_fn
        self._set_history_queries_fn = set_history_queries_fn
        self._get_history_index_fn = get_history_index_fn
        self._set_history_index_fn = set_history_index_fn
        self._get_current_draft_fn = get_current_draft_fn
        self._set_current_draft_fn = set_current_draft_fn
        self._get_ai_markdown_text_fn = get_ai_markdown_text_fn

        # State
        self._ai_pending_image_hash = None
        self._ai_pending_image_path = None
        self._ai_pending_image_data_uri = None
        self._ai_cmd_popover = None
        self._ai_model_popover = None
        self._ai_model_listbox = None

        self._build_ui()

    def _build_ui(self):
        """构建输入区域 UI。"""
        # Sub-agent status bar (shown when background sub-agents exist)
        self._ai_subagent_bar = Gtk.FlowBox.new()
        self._ai_subagent_bar.set_max_children_per_line(100)
        self._ai_subagent_bar.set_min_children_per_line(1)
        self._ai_subagent_bar.set_selection_mode(Gtk.SelectionMode.NONE)
        self._ai_subagent_bar.set_column_spacing(6)
        self._ai_subagent_bar.set_row_spacing(0)
        self._ai_subagent_bar.set_margin_bottom(2)
        self._ai_subagent_bar.set_margin_start(4)
        self._ai_subagent_bar.set_margin_end(4)
        self._ai_subagent_bar.hide()
        self._ai_subagent_bar.set_no_show_all(True)
        self._ai_subagent_bar.get_style_context().add_class("subagent-status-bar")
        self._ai_subagent_bar.connect("child-activated", self._on_subagent_child_activated)
        self.pack_start(self._ai_subagent_bar, False, False, 0)

        # Input text view
        self._ai_entry = Gtk.TextView.new()
        self._ai_entry.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._ai_entry.set_hexpand(True)
        self._ai_entry.set_left_margin(6)
        self._ai_entry.set_right_margin(6)
        self._ai_entry.set_top_margin(4)
        self._ai_entry.set_bottom_margin(4)
        self._ai_entry.set_accepts_tab(False)
        self._ai_entry.get_buffer().connect("changed", lambda *_: self._adjust_entry_height())
        self._ai_entry.get_buffer().connect("changed", lambda *_: self._on_entry_changed())
        self._ai_entry.placeholder_text = "输入后续问题..."
        self._ai_entry.connect_after("draw", self._textview_draw_placeholder)
        self._ai_entry.connect("key-press-event", self._on_key_press)
        self._ai_entry.connect("button-press-event", self._on_button_press)
        self._ai_entry.connect("paste-clipboard", self._on_paste_clipboard)

        # Drag and Drop support for files
        self._ai_entry.drag_dest_set(
            Gtk.DestDefaults.ALL,
            [],
            Gdk.DragAction.COPY
        )
        self._ai_entry.drag_dest_add_uri_targets()
        self._ai_entry.connect("drag-data-received", self._on_drag_data_received)

        self._ai_entry_sw = Gtk.ScrolledWindow.new()
        self._ai_entry_sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._ai_entry_sw.add(self._ai_entry)

        # Buttons row
        self._ai_send_btn = Gtk.Button.new_with_label("发送")
        self._ai_send_btn.connect("clicked", lambda *_: self._on_send_clicked_btn())

        self._ai_new_btn = Gtk.Button.new_with_label("+")
        self._ai_new_btn.set_tooltip_text("新对话 (Ctrl+Shift+N)")
        self._ai_new_btn.set_size_request(32, -1)
        self._ai_new_btn.get_style_context().add_class("flat")
        self._ai_new_btn.connect("clicked", lambda *_: self._on_new_conversation_cb() if self._on_new_conversation_cb else None)

        self._ai_attach_btn = Gtk.Button.new_with_label("\U0001f4ce")
        self._ai_attach_btn.set_tooltip_text("添加图片附件")
        self._ai_attach_btn.set_size_request(32, -1)
        self._ai_attach_btn.get_style_context().add_class("flat")
        self._ai_attach_btn.connect("clicked", self._on_attach_btn_clicked)

        self._ai_input_row = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        self._ai_input_row.pack_start(self._ai_new_btn, False, False, 0)
        self._ai_input_row.pack_start(self._ai_entry_sw, True, True, 0)
        self._ai_input_row.pack_start(self._ai_attach_btn, False, False, 0)
        self._ai_input_row.pack_start(self._ai_send_btn, False, False, 0)
        self.pack_start(self._ai_input_row, False, False, 0)

        # Attachment bar for pending image
        self._ai_attachment_bar = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        self._ai_attachment_bar.set_no_show_all(True)
        self._ai_attachment_bar.set_margin_bottom(4)
        self._ai_attachment_bar.set_margin_start(4)
        self._ai_attach_thumb = Gtk.Image.new()
        self._ai_attach_label = Gtk.Label.new("")
        self._ai_attach_label.set_opacity(0.7)
        self._ai_attach_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._ai_attach_remove_btn = Gtk.Button.new_with_label("\u00d7")
        self._ai_attach_remove_btn.set_relief(Gtk.ReliefStyle.NONE)
        self._ai_attach_remove_btn.set_size_request(24, 24)
        self._ai_attach_remove_btn.connect("clicked", lambda *_: self._remove_pending_image())
        self._ai_attachment_bar.pack_start(self._ai_attach_thumb, False, False, 0)
        self._ai_attachment_bar.pack_start(self._ai_attach_label, True, True, 0)
        self._ai_attachment_bar.pack_start(self._ai_attach_remove_btn, False, False, 0)
        self.pack_start(self._ai_attachment_bar, False, False, 0)

        # Model selector popover
        self._build_model_popover()

        # Command popover
        self._ai_cmd_popover = AICommandPopover(self._ai_entry, self._AI_COMMANDS)

        # ── 输入框下方状态栏 ──
        self._ai_hint_label = Gtk.Label.new("")
        self._ai_hint_label.set_xalign(1)
        self._ai_hint_label.get_style_context().add_class("dim-label")
        self._ai_hint_label.set_margin_end(4)
        self._ai_hint_label.set_opacity(0.6)
        self.update_hint_label(0)
        self.pack_start(self._ai_hint_label, False, False, 0)

    def _build_model_popover(self):
        """构建模型选择 Popover。"""
        self._ai_model_popover = Gtk.Popover.new(self._ai_entry)
        self._ai_model_popover.set_position(Gtk.PositionType.TOP)
        self._ai_model_popover.get_style_context().add_class("model-selector-popover")

        model_sw = Gtk.ScrolledWindow.new()
        model_sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        model_sw.set_min_content_height(200)
        model_sw.set_max_content_height(440)
        model_sw.set_size_request(400, 200)

        self._ai_model_listbox = Gtk.ListBox.new()
        self._ai_model_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._ai_model_listbox.set_activate_on_single_click(True)
        self._ai_model_listbox.get_style_context().add_class("model-selector-list")
        self._ai_model_listbox.connect("row-activated", self._on_model_selector_activated)

        model_sw.add(self._ai_model_listbox)
        self._ai_model_popover.add(model_sw)
        self._ai_model_popover.connect("closed", self._on_model_popover_closed)

    # ── 占位符绘制 ───────────────────────────────────────────────

    @staticmethod
    def _textview_draw_placeholder(textview, cr):
        """Draw placeholder text for Gtk.TextView."""
        try:
            buf = textview.get_buffer()
            if buf.get_char_count() == 0:
                placeholder = getattr(textview, "placeholder_text", "")
                if placeholder:
                    text_window = textview.get_window(Gtk.TextWindowType.TEXT)
                    if text_window and Gtk.cairo_should_draw_window(cr, text_window):
                        cr.save()
                        start_iter = buf.get_start_iter()
                        rect = textview.get_iter_location(start_iter)
                        left, top = textview.buffer_to_window_coords(Gtk.TextWindowType.TEXT, rect.x, rect.y)
                        cr.translate(left, top)
                        layout = textview.create_pango_layout(placeholder)
                        context = textview.get_style_context()
                        font_desc = context.get_property("font", Gtk.StateFlags.NORMAL)
                        layout.set_font_description(font_desc)
                        color = context.get_color(Gtk.StateFlags.NORMAL)
                        cr.set_source_rgba(color.red, color.green, color.blue, 0.45)
                        PangoCairo.show_layout(cr, layout)
                        cr.restore()
        except Exception:
            pass
        return False

    # ── 输入框高度自适应 ─────────────────────────────────────────

    def _adjust_entry_height(self):
        """根据内容自动调整输入框高度。"""
        buf = self._ai_entry.get_buffer()
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        text = buf.get_text(start, end, True)

        newline_count = text.count('\n')
        target_lines = min(max(1, newline_count + 1), 5)

        layout = self._ai_entry.create_pango_layout("Ag")
        _, logical = layout.get_pixel_extents()
        line_height = logical.height + 2
        margin_px = self._ai_entry.get_top_margin() + self._ai_entry.get_bottom_margin()
        height = int(target_lines * line_height + margin_px)

        self._ai_entry_sw.set_size_request(-1, height)
        self._ai_entry.queue_resize()

    # ── 发送按钮 ─────────────────────────────────────────────────

    def update_send_button(self, sending: bool, sensitive: bool = True):
        """Switch the send button between '发送' (idle) and '暂停' (streaming)."""
        self._ai_send_btn.set_label(AI_BTN_LABEL_STOP if sending else AI_BTN_LABEL_SEND)
        self._ai_send_btn.set_sensitive(sensitive)

    def set_send_button_sensitive(self, sensitive: bool):
        self._ai_send_btn.set_sensitive(sensitive)

    def update_hint_label(self, token_count: int = 0):
        """更新输入框下方的提示标签。"""
        label = f"Shift+Enter \u21b5 \u00b7 Enter \u53d1\u9001"
        if token_count > 0:
            label = f"\U0001f4dd {token_count:,} tokens  |  " + label
        if hasattr(self, "_ai_hint_label"):
            self._ai_hint_label.set_text(label)

    # ── 获取/设置输入文本 ───────────────────────────────────────

    def get_text(self) -> str:
        """获取输入框文本。"""
        buf = self._ai_entry.get_buffer()
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        return buf.get_text(start, end, True).strip()

    def set_text(self, text: str):
        """设置输入框文本并移动光标到末尾。"""
        buf = self._ai_entry.get_buffer()
        buf.set_text(text)
        buf.place_cursor(buf.get_end_iter())

    def clear_text(self):
        """清空输入框。"""
        buf = self._ai_entry.get_buffer()
        buf.set_text("")

    def grab_focus_entry(self):
        """聚焦到输入框。"""
        self._ai_entry.grab_focus()

    def set_placeholder(self, text: str):
        """设置输入框占位文本。"""
        self._ai_entry.placeholder_text = text
        self._ai_entry.queue_draw()

    # ── 附件管理 ─────────────────────────────────────────────────

    def set_pending_image(self, img_hash: str, img_path: str, data_uri: str):
        self._ai_pending_image_hash = img_hash
        self._ai_pending_image_path = img_path
        self._ai_pending_image_data_uri = data_uri
        self._show_attachment_bar()

    def _remove_pending_image(self):
        self._ai_pending_image_hash = None
        self._ai_pending_image_path = None
        self._ai_pending_image_data_uri = None
        self._hide_attachment_bar()

    def get_pending_image(self) -> Optional[Dict]:
        """Return pending image info dict or None."""
        if self._ai_pending_image_hash:
            return {
                "hash": self._ai_pending_image_hash,
                "path": self._ai_pending_image_path,
                "data_uri": self._ai_pending_image_data_uri,
            }
        return None

    def _show_attachment_bar(self):
        if not self._ai_pending_image_path or not os.path.isfile(self._ai_pending_image_path):
            return
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                self._ai_pending_image_path, 60, 60
            )
            self._ai_attach_thumb.set_from_pixbuf(pixbuf)
        except Exception:
            self._ai_attach_thumb.clear()
        fname = os.path.basename(self._ai_pending_image_path)
        self._ai_attach_label.set_text(f"📎 {fname}")

        self._ai_attachment_bar.show()
        self._ai_attach_thumb.show()
        self._ai_attach_label.show()
        self._ai_attach_remove_btn.show()
        self.queue_resize()

    def _hide_attachment_bar(self):
        self._ai_attachment_bar.hide()
        self.queue_resize()

    # ── 附件按钮 ─────────────────────────────────────────────────

    def _on_attach_btn_clicked(self, _btn):
        """打开文件选择对话框选择图片。"""
        dialog = Gtk.FileChooserDialog(
            title="选择图片",
            parent=self.get_toplevel(),
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.ACCEPT
        )

        if self._on_dialog_shown_cb:
            dialog.connect("show", lambda *_: self._on_dialog_shown_cb())
        if self._on_dialog_hidden_cb:
            dialog.connect("destroy", lambda *_: self._on_dialog_hidden_cb())

        filter_image = Gtk.FileFilter()
        filter_image.set_name("图片文件 (png/jpg/jpeg/webp)")
        filter_image.add_mime_type("image/png")
        filter_image.add_mime_type("image/jpeg")
        filter_image.add_mime_type("image/webp")
        dialog.add_filter(filter_image)

        response = dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            filename = dialog.get_filename()
            dialog.destroy()
            if filename:
                self._attach_image_from_file(filename)
        else:
            dialog.destroy()

    def _attach_image_from_file(self, filepath: str):
        """后台线程：加载图片并设置 pending。"""
        def do_background_attach():
            try:
                with open(filepath, "rb") as f:
                    image_data = f.read()
                h = hashlib.sha256(image_data).hexdigest()[:16]
                img_dir = os.path.join(CONFIG_DIR, "images")
                os.makedirs(img_dir, exist_ok=True)
                ext = os.path.splitext(filepath)[1].lower()
                if ext not in [".png", ".jpg", ".jpeg", ".webp"]:
                    ext = ".png"
                img_path = os.path.join(img_dir, f"{h}{ext}")
                if not os.path.exists(img_path):
                    with open(img_path, "wb") as f:
                        f.write(image_data)
                data_uri = _image_to_data_uri(img_path)
                if data_uri:
                    GLib.idle_add(self.set_pending_image, h, img_path, data_uri)
            except Exception:
                pass

        threading.Thread(target=do_background_attach, daemon=True).start()

    # ── 剪贴板图片 ───────────────────────────────────────────────

    def _on_paste_clipboard(self, entry):
        """Fires on any paste operation. Schedules async check for clipboard image."""
        GLib.idle_add(self._async_check_clipboard_image)
        return False

    def _async_check_clipboard_image(self):
        threading.Thread(target=self._do_capture_clipboard_image, daemon=True).start()
        return False

    def _do_capture_clipboard_image(self):
        """检查剪贴板中是否有图片并设置 pending。"""
        from stores.clipboard_store import _capture_image
        image_data = _capture_image()
        if not image_data:
            return
        h = hashlib.sha256(image_data).hexdigest()[:16]
        img_dir = os.path.join(CONFIG_DIR, "images")
        try:
            os.makedirs(img_dir, exist_ok=True)
            img_path = os.path.join(img_dir, f"{h}.png")
            if not os.path.exists(img_path):
                with open(img_path, "wb") as f:
                    f.write(image_data)
            data_uri = _image_to_data_uri(img_path)
            if data_uri:
                GLib.idle_add(self.set_pending_image, h, img_path, data_uri)
        except Exception:
            pass

    # ── 拖拽 ─────────────────────────────────────────────────────

    def _on_drag_data_received(self, widget, context, x, y, selection_data, info, time):
        """处理文件拖拽。"""
        uris = selection_data.get_uris()
        if uris:
            for uri in uris:
                parsed = urlparse(uri)
                if parsed.scheme == "file":
                    filepath = urllib.parse.unquote(parsed.path)
                    mime_type, _ = mimetypes.guess_type(filepath)
                    if mime_type and mime_type.startswith("image/"):
                        self._attach_image_from_file(filepath)
                        widget.stop_emission_by_name("drag-data-received")
                        context.finish(True, False, time)
                        return
        context.finish(False, False, time)

    # ── 子代理状态栏 ─────────────────────────────────────────────

    def _on_subagent_child_activated(self, flowbox, child):
        """Handle child activation signal from FlowBox to toggle selection."""
        # Delegated to panel via callback
        pass

    # ── 键盘事件 ─────────────────────────────────────────────────

    def _on_key_press(self, widget, event):
        """处理输入框键盘事件。"""
        keyname = Gdk.keyval_name(event.keyval)
        is_shift = (event.state & Gdk.ModifierType.SHIFT_MASK) != 0
        is_ctrl = (event.state & Gdk.ModifierType.CONTROL_MASK) != 0

        # Handle command popover navigation
        if self._ai_cmd_popover is not None and self._ai_cmd_popover.is_visible():
            return self._handle_cmd_popover_key(keyname)

        # Tab completion for commands
        if keyname == "Tab":
            buf = self._ai_entry.get_buffer()
            start = buf.get_start_iter()
            end = buf.get_end_iter()
            text = buf.get_text(start, end, True).strip()
            if text.startswith("/") and " " not in text:
                search = text.lstrip("/")
                matches = [cmd for cmd, _ in self._AI_COMMANDS if cmd.startswith("/" + search)]
                if len(matches) == 1:
                    buf.set_text(matches[0] + " ")
                    buf.place_cursor(buf.get_end_iter())
                    return True
                elif len(matches) > 1:
                    self._ai_cmd_popover.rebuild(text)
                    return True
            return False

        # Ctrl+L: reset panel
        if is_ctrl and keyname in ("l", "L"):
            if self._on_delete_command_cb:
                self._on_delete_command_cb()
            return True

        # History navigation with Up/Down
        if keyname in ("Up", "KP_Up", "Down", "KP_Down"):
            buf = self._ai_entry.get_buffer()
            start = buf.get_start_iter()
            end = buf.get_end_iter()
            text_val = buf.get_text(start, end, True)
            cursor_iter = buf.get_iter_at_mark(buf.get_insert())
            cursor_line = cursor_iter.get_line()
            total_lines = buf.get_line_count()

            history_queries = self._get_history_queries_fn() if self._get_history_queries_fn else []
            history_index = self._get_history_index_fn() if self._get_history_index_fn else -1
            current_draft = self._get_current_draft_fn() if self._get_current_draft_fn else ""

            if keyname in ("Up", "KP_Up") and cursor_line == 0:
                if history_queries:
                    if history_index == -1:
                        self._set_current_draft(text_val)
                        history_index = len(history_queries) - 1
                    elif history_index > 0:
                        history_index -= 1
                    if self._set_history_index_fn:
                        self._set_history_index_fn(history_index)
                    hist_text = history_queries[history_index]
                    buf.set_text(hist_text)
                    buf.place_cursor(buf.get_end_iter())
                    return True
            elif keyname in ("Down", "KP_Down") and cursor_line == total_lines - 1:
                if history_index != -1:
                    if history_index < len(history_queries) - 1:
                        history_index += 1
                        if self._set_history_index_fn:
                            self._set_history_index_fn(history_index)
                        hist_text = history_queries[history_index]
                        buf.set_text(hist_text)
                    else:
                        if self._set_history_index_fn:
                            self._set_history_index_fn(-1)
                        buf.set_text(self._get_current_draft_fn() if self._get_current_draft_fn else "")
                    buf.place_cursor(buf.get_end_iter())
                    return True

        # Enter handling
        is_enter = keyname in ("Return", "KP_Enter")
        if not is_enter:
            return False

        # Shift+Enter → newline
        if is_shift and not is_ctrl:
            return False

        # Enter → send
        try:
            self._on_send_clicked_btn()
        except Exception as e:
            print(f"[key-press] send error: {e}", flush=True)
        return True

    def _handle_cmd_popover_key(self, keyname: str) -> bool:
        """处理命令弹窗中的键盘导航。"""
        if keyname in ("Up", "KP_Up"):
            current = self._ai_cmd_popover.listbox.get_selected_row()
            if current:
                idx = current.get_index()
                if idx > 0:
                    above = self._ai_cmd_popover.listbox.get_row_at_index(idx - 1)
                    if above:
                        self._ai_cmd_popover.listbox.select_row(above)
                        self._ai_cmd_popover.scroll_to_row(above)
            return True
        if keyname in ("Down", "KP_Down"):
            current = self._ai_cmd_popover.listbox.get_selected_row()
            if current:
                idx = current.get_index()
                below = self._ai_cmd_popover.listbox.get_row_at_index(idx + 1)
                if below:
                    self._ai_cmd_popover.listbox.select_row(below)
                    self._ai_cmd_popover.scroll_to_row(below)
            else:
                first = self._ai_cmd_popover.listbox.get_row_at_index(0)
                if first:
                    self._ai_cmd_popover.listbox.select_row(first)
                    self._ai_cmd_popover.scroll_to_row(first)
            return True
        if keyname in ("Return", "KP_Enter"):
            self._ai_cmd_popover.confirm_command_completion()
            return True
        if keyname == "Tab":
            self._ai_cmd_popover.confirm_command_completion()
            return True
        if keyname == "Escape":
            self._ai_cmd_popover.dismiss()
            return True
        return False

    # ── 右键菜单 ─────────────────────────────────────────────────

    def _on_button_press(self, widget, event):
        """处理右键菜单。"""
        if event.button != 3:
            return False
        menu = Gtk.Menu.new()
        paste_item = Gtk.MenuItem.new_with_label("粘贴")
        paste_item.connect("activate", lambda *_: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).request_text(
            lambda clip, text: widget.get_buffer().insert_at_cursor(text if text else "")
        ))
        menu.append(paste_item)
        copy_item = Gtk.MenuItem.new_with_label("复制")
        copy_item.connect("activate", lambda *_: widget.emit("copy-clipboard"))
        menu.append(copy_item)
        select_all = Gtk.MenuItem.new_with_label("全选")
        select_all.connect("activate", lambda *_: widget.emit("select-all", True))
        menu.append(select_all)
        if self._on_menu_shown_cb:
            self._on_menu_shown_cb()
        menu.connect("deactivate", lambda *_: GLib.timeout_add(300, self._on_menu_deactivated))
        menu.show_all()
        menu.popup(None, None, None, None, event.button, event.time)
        return True

    def _on_menu_deactivated(self):
        if self._on_menu_hidden_cb:
            self._on_menu_hidden_cb()
        return False

    # ── 输入变更 ─────────────────────────────────────────────────

    def _on_entry_changed(self):
        """输入框内容变更时触发命令弹窗。"""
        buf = self._ai_entry.get_buffer()
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        raw_text = buf.get_text(start, end, True)
        text = raw_text.strip()

        if text.startswith("/") and (" " not in text or text.startswith("/skill")):
            self._ai_cmd_popover.rebuild(text)
        elif text.startswith("skill:"):
            self._ai_cmd_popover.rebuild(text)
        else:
            self._ai_cmd_popover.dismiss()

        if self._on_entry_changed_cb:
            self._on_entry_changed_cb(raw_text)

    # ── 发送逻辑 ─────────────────────────────────────────────────

    def _on_send_clicked_btn(self):
        """发送按钮点击/Enter 触发。委托给父面板处理。"""
        if self._on_send_clicked_cb:
            self._on_send_clicked_cb()

    # ── 模型选择器 ───────────────────────────────────────────────

    def show_model_selector(self):
        """显示模型选择弹窗。"""
        llm_store = self._get_llm_settings_store_fn() if self._get_llm_settings_store_fn else None
        active_info = self._get_active_model_info_fn() if self._get_active_model_info_fn else None
        if not llm_store:
            return

        for old in self._ai_model_listbox.get_children():
            self._ai_model_listbox.remove(old)

        for m in llm_store.models:
            row = Gtk.ListBoxRow()
            row.model_alias = m.alias
            hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
            hbox.set_margin_start(8)
            hbox.set_margin_end(8)
            hbox.set_margin_top(6)
            hbox.set_margin_bottom(6)

            name_lbl = Gtk.Label.new(m.alias)
            name_lbl.set_xalign(0)
            name_lbl.set_markup(f"<b>{m.alias}</b>")
            if m.is_default:
                default_lbl = Gtk.Label.new("(默认)")
                default_lbl.get_style_context().add_class("model-default-tag")
                default_lbl.set_opacity(0.9)
                hbox.pack_start(default_lbl, False, False, 0)

            detail_lbl = Gtk.Label.new(m.model_name)
            detail_lbl.set_xalign(1)
            detail_lbl.set_opacity(0.6)

            hbox.pack_start(name_lbl, True, True, 0)
            hbox.pack_start(detail_lbl, False, False, 0)
            row.add(hbox)
            self._ai_model_listbox.add(row)

        self._ai_model_listbox.show_all()
        current_alias = (active_info or {}).get("alias")
        target_row = None
        if current_alias:
            for child in self._ai_model_listbox.get_children():
                if getattr(child, "model_alias", None) == current_alias:
                    target_row = child
                    break
        if not target_row:
            target_row = self._ai_model_listbox.get_row_at_index(0)
        if target_row:
            self._ai_model_listbox.select_row(target_row)

        child = self._ai_model_popover.get_child()
        if child:
            child.show_all()
        self._ai_model_popover.popup()
        self._ai_model_listbox.grab_focus()

    def hide_model_selector(self):
        """隐藏模型选择弹窗。"""
        if not self._ai_model_popover.get_visible():
            return
        self._ai_model_popover.popdown()

    def _on_model_popover_closed(self, popover):
        """模型选择弹窗关闭后聚焦输入框。"""
        self._ai_entry.grab_focus()

    def _on_model_selector_activated(self, listbox, row):
        """模型选择回调。"""
        if not row:
            return
        alias = row.model_alias
        self.hide_model_selector()
        # Delegate to panel
        if self._on_model_command_cb:
            self._on_model_command_cb(alias)
