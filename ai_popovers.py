import re
from gi.repository import Gtk, Gdk, GLib, Pango
from typing import Optional, Callable, Set, List, Dict, Tuple, Any
from ai_text_utils import _clean_history_title

class AICommandPopover(Gtk.Popover):
    def __init__(self, relative_to_entry, command_list: List[Tuple[str, str]]):
        super().__init__(relative_to=relative_to_entry)
        self.entry = relative_to_entry
        self.command_list = command_list
        self.get_style_context().add_class("command-autocomplete-popover")
        self.set_position(Gtk.PositionType.TOP)

        self._ai_cmd_popover_visible = False
        self._ai_cmd_suppress_rebuild = False

        self.build_ui()

    def build_ui(self):
        cmd_sw = Gtk.ScrolledWindow.new()
        cmd_sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        cmd_sw.set_min_content_height(100)
        cmd_sw.set_max_content_height(300)

        self.listbox = Gtk.ListBox.new()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(False)
        self.listbox.get_style_context().add_class("command-autocomplete-list")
        self.listbox.connect("row-activated", self._on_cmd_row_activated)

        cmd_sw.add(self.listbox)
        self.add(cmd_sw)
        self.connect("closed", self._on_cmd_popover_closed)
        self.connect("key-press-event", self._on_cmd_popover_key_press)

    def _on_cmd_popover_closed(self, _popover):
        self._ai_cmd_popover_visible = False

    def is_visible(self) -> bool:
        return self._ai_cmd_popover_visible

    def rebuild(self, prefix: str):
        if self._ai_cmd_suppress_rebuild:
            return
        search = prefix.lstrip("/")
        matches = [
            (cmd, desc) for cmd, desc in self.command_list
            if cmd.startswith("/" + search)
        ]
        if not matches:
            self.dismiss()
            return

        self.listbox.handler_block_by_func(self._on_cmd_row_activated)
        for row in self.listbox.get_children():
            self.listbox.remove(row)

        for cmd, desc in matches:
            row = Gtk.ListBoxRow.new()
            lbl = Gtk.Label.new(f"{cmd}  —  {desc}")
            lbl.set_xalign(0)
            lbl.set_margin_start(8)
            lbl.set_margin_end(8)
            lbl.set_margin_top(6)
            lbl.set_margin_bottom(6)
            row.add(lbl)
            row._cmd_command = cmd
            self.listbox.add(row)

        self.listbox.show_all()
        first = self.listbox.get_row_at_index(0)
        if first:
            self.listbox.select_row(first)
        self.listbox.handler_unblock_by_func(self._on_cmd_row_activated)

        if not self._ai_cmd_popover_visible:
            child = self.get_child()
            if child:
                entry_width = self.entry.get_allocated_width()
                min_width = 180
                child.set_size_request(max(entry_width, min_width), -1)
                child.show_all()
            self.popup()
            self._ai_cmd_popover_visible = True

    def _on_cmd_popover_key_press(self, _popover, event):
        keyname = Gdk.keyval_name(event.keyval)
        state = event.state
        is_ctrl = (state & Gdk.ModifierType.CONTROL_MASK) != 0
        is_alt = (state & Gdk.ModifierType.MOD1_MASK) != 0

        if keyname in ("Up", "KP_Up"):
            current = self.listbox.get_selected_row()
            if current:
                above = current.get_prev_sibling()
                if above:
                    self.listbox.select_row(above)
            return True

        if keyname in ("Down", "KP_Down"):
            current = self.listbox.get_selected_row()
            if current:
                below = current.get_next_sibling()
                if below:
                    self.listbox.select_row(below)
            else:
                first = self.listbox.get_row_at_index(0)
                if first:
                    self.listbox.select_row(first)
            return True

        if keyname in ("Return", "KP_Enter", "Tab"):
            self.confirm_command_completion()
            return True

        if keyname == "Escape":
            self.dismiss()
            return True

        if keyname == "BackSpace":
            buf = self.entry.get_buffer()
            if buf.get_selection_bounds():
                buf.delete_selection(True, True)
                return True
            cursor = buf.get_iter_at_mark(buf.get_insert())
            if cursor.get_offset() > 0:
                cursor.backward_chars(1)
                buf.delete(cursor, buf.get_iter_at_mark(buf.get_insert()))
            return True

        if keyname == "Delete":
            buf = self.entry.get_buffer()
            if buf.get_selection_bounds():
                buf.delete_selection(True, True)
                return True
            cursor = buf.get_iter_at_mark(buf.get_insert()).copy()
            end = buf.get_end_iter()
            if cursor.get_offset() < end.get_offset():
                cursor.forward_chars(1)
                buf.delete(buf.get_iter_at_mark(buf.get_insert()), cursor)
            return True

        if not is_ctrl and not is_alt and len(keyname) == 1:
            buf = self.entry.get_buffer()
            buf.insert_at_cursor(keyname)
            return True

        return True

    def dismiss(self):
        if self._ai_cmd_popover_visible:
            self.popdown()
            self._ai_cmd_popover_visible = False
        self.entry.grab_focus()

    def _on_cmd_row_activated(self, _listbox, row):
        if row is not None:
            self.confirm_command_completion()

    def confirm_command_completion(self):
        selected = self.listbox.get_selected_row()
        if selected is None:
            return
        command = getattr(selected, "_cmd_command", None)
        if not command:
            lbl = selected.get_child()
            raw = lbl.get_text() if isinstance(lbl, Gtk.Label) else ""
            command = raw.split("  ")[0].strip()
        if not command:
            return

        self._ai_cmd_suppress_rebuild = True
        buf = self.entry.get_buffer()
        buf.set_text(command + " ")
        end = buf.get_end_iter()
        buf.place_cursor(end)
        self._ai_cmd_suppress_rebuild = False
        self.dismiss()


class HistoryPopover(Gtk.Popover):
    def __init__(self, relative_to_widget, history_btn, history_btn_label, conversation_store,
                 get_current_conv_id_fn: Callable[[], Optional[str]],
                 get_sorted_conversations_fn: Callable[[], List[Dict[str, Any]]],
                 on_conversation_selected: Callable[[str], None],
                 on_clear_all_deleted_reset_fn: Callable[[], None],
                 on_dialog_shown: Optional[Callable[[], None]],
                 on_dialog_hidden: Optional[Callable[[], None]],
                 on_popover_shown: Optional[Callable[[], None]],
                 on_popover_closed: Optional[Callable[[], None]]):
        super().__init__(relative_to=relative_to_widget)
        self.history_btn = history_btn
        self.history_btn_label = history_btn_label
        self.conversation_store = conversation_store
        self.get_current_conv_id_fn = get_current_conv_id_fn
        self.get_sorted_conversations_fn = get_sorted_conversations_fn
        self.on_conversation_selected = on_conversation_selected
        self.on_clear_all_deleted_reset_fn = on_clear_all_deleted_reset_fn
        self.on_dialog_shown = on_dialog_shown
        self.on_dialog_hidden = on_dialog_hidden
        self.on_popover_shown_cb = on_popover_shown
        self.on_popover_closed_cb = on_popover_closed

        self.get_style_context().add_class("ai-history-popover")
        self.set_position(Gtk.PositionType.BOTTOM)
        self.connect("closed", self._on_popover_closed)

        self._ai_history_switching = False
        self._ai_history_edit_mode = False
        self._ai_history_selected_ids = set()

        self.build_ui()
        self.history_btn.connect("clicked", self._on_history_btn_clicked)

    def build_ui(self):
        popover_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 2)
        
        popover_scrolled = Gtk.ScrolledWindow.new(None, None)
        popover_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        popover_scrolled.set_size_request(240, 260)
        
        self.listbox = Gtk.ListBox.new()
        self.listbox.connect("row-activated", self._on_history_row_activated)
        
        popover_scrolled.add(self.listbox)
        popover_vbox.pack_start(popover_scrolled, True, True, 0)
        
        popover_vbox.pack_start(Gtk.Separator.new(Gtk.Orientation.HORIZONTAL), False, False, 2)
        
        self.toolbar_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 2)
        
        self.normal_toolbar = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        self.edit_btn = Gtk.Button.new_with_label("编辑")
        self.edit_btn.get_style_context().add_class("edit-mode-btn")
        self.edit_btn.set_size_request(60, -1)
        self.clear_all_btn = Gtk.Button.new_with_label("🗑️ 清除所有历史")
        self.clear_all_btn.get_style_context().add_class("clear-all-btn")
        self.clear_all_btn.connect("clicked", self._on_clear_all_history_clicked)
        self.normal_toolbar.pack_start(self.edit_btn, False, False, 0)
        self.normal_toolbar.pack_start(self.clear_all_btn, True, True, 0)
        
        self.edit_toolbar = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        self.select_all_btn = Gtk.Button.new_with_label("☐ 全选")
        self.select_all_btn.get_style_context().add_class("edit-mode-btn")
        self.select_all_btn.set_size_request(68, -1)
        self.delete_sel_btn = Gtk.Button.new_with_label("删除选中 (0)")
        self.delete_sel_btn.get_style_context().add_class("delete-sel-btn")
        self.delete_sel_btn.set_sensitive(False)
        self.done_btn = Gtk.Button.new_with_label("完成")
        self.done_btn.get_style_context().add_class("edit-mode-btn")
        self.done_btn.set_size_request(56, -1)
        self.edit_toolbar.pack_start(self.select_all_btn, False, False, 0)
        self.edit_toolbar.pack_start(self.delete_sel_btn, True, True, 0)
        self.edit_toolbar.pack_start(self.done_btn, False, False, 0)
        
        self.edit_btn.connect("clicked", lambda *_: self._enter_edit_mode())
        self.select_all_btn.connect("clicked", self._on_select_all_clicked)
        self.delete_sel_btn.connect("clicked", self._on_delete_selected_clicked)
        self.done_btn.connect("clicked", lambda *_: self._exit_edit_mode())
        
        self.toolbar_box.pack_start(self.normal_toolbar, False, False, 0)
        self.toolbar_box.pack_start(self.edit_toolbar, False, False, 0)
        
        popover_vbox.pack_start(self.toolbar_box, False, False, 2)
        
        self.add(popover_vbox)
        popover_vbox.show_all()
        self.edit_toolbar.hide()

    def _on_history_btn_clicked(self, btn):
        if self.get_visible():
            self.popdown()
        else:
            self.refresh_dropdown()
            self.show_all()
            self.edit_toolbar.hide()
            self.popup()
            if self.on_popover_shown_cb:
                self.on_popover_shown_cb()

    def _on_popover_closed(self, _popover):
        if self._ai_history_edit_mode:
            self._exit_edit_mode()
        if self.on_popover_closed_cb:
            self.on_popover_closed_cb()

    def _on_history_row_activated(self, _listbox, row):
        if not row:
            return
        conv_id = getattr(row, "conversation_id", None)
        if not conv_id:
            return
        
        if self._ai_history_edit_mode:
            check = getattr(row, "check_button", None)
            if check:
                check.set_active(not check.get_active())
            return
        
        self.popdown()
        
        current_conv_id = self.get_current_conv_id_fn()
        if conv_id == current_conv_id:
            return
            
        def defer_switch():
            self.on_conversation_selected(conv_id)
            return False
        GLib.idle_add(defer_switch)

    def _on_clear_all_history_clicked(self, _btn):
        summaries = self.conversation_store.list_conversations()
        if not summaries:
            return
            
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text="确定要清除所有 AI 对话历史吗？",
        )
        dialog.format_secondary_text("此操作将永久删除所有历史会话记录（共 %d 条），且无法恢复。" % len(summaries))
        
        def on_resp(dlg, resp):
            dlg.destroy()
            if resp == Gtk.ResponseType.YES:
                self.popdown()
                for s in summaries:
                    sid = s.get("id")
                    if sid:
                        self.conversation_store.delete_conversation(sid)
                self.on_clear_all_deleted_reset_fn()
            if self.on_dialog_hidden:
                self.on_dialog_hidden()
                
        dialog.connect("response", on_resp)
        if self.on_dialog_shown:
            self.on_dialog_shown()
        dialog.show_all()

    def _enter_edit_mode(self):
        self._ai_history_edit_mode = True
        self.refresh_dropdown(edit_mode=True)
        self.show_all()
        self.normal_toolbar.hide()
        self.edit_toolbar.show_all()
        self._update_delete_sel_btn_label()

    def _exit_edit_mode(self):
        self._ai_history_edit_mode = False
        self._ai_history_selected_ids.clear()
        self.refresh_dropdown()
        self.show_all()
        self.edit_toolbar.hide()
        self.normal_toolbar.show_all()

    def _on_edit_check_toggled(self, check, conv_id):
        if not conv_id:
            return
        if check.get_active():
            self._ai_history_selected_ids.add(conv_id)
        else:
            self._ai_history_selected_ids.discard(conv_id)
        self._update_delete_sel_btn_label()

    def _update_delete_sel_btn_label(self):
        n = len(self._ai_history_selected_ids)
        self.delete_sel_btn.set_label(f"删除选中 ({n})")
        self.delete_sel_btn.set_sensitive(n > 0)
        rows = self.listbox.get_children()
        all_selected = all(
            getattr(row, "conversation_id", None) in self._ai_history_selected_ids
            for row in rows if getattr(row, "conversation_id", None)
        ) if rows else False
        self.select_all_btn.set_label("☑ 全选" if all_selected else "☐ 全选")

    def _on_select_all_clicked(self, _btn=None):
        summaries = self.get_sorted_conversations_fn()
        ids = [s.get("id") for s in summaries if s.get("id")]
        if not ids:
            return
        all_selected = all(cid in self._ai_history_selected_ids for cid in ids)
        if all_selected:
            self._ai_history_selected_ids.clear()
        else:
            self._ai_history_selected_ids = set(ids)
        for row in self.listbox.get_children():
            conv_id = getattr(row, "conversation_id", None)
            check = getattr(row, "check_button", None)
            if check and conv_id:
                check.set_active(conv_id in self._ai_history_selected_ids)
        self._update_delete_sel_btn_label()

    def _on_delete_selected_clicked(self, _btn=None):
        selected = list(self._ai_history_selected_ids)
        if not selected:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text="确定要删除选中的 %d 条对话历史吗？" % len(selected),
        )
        dialog.format_secondary_text("此操作将永久删除所选历史会话记录，且无法恢复。")

        def on_resp(dlg, resp):
            dlg.destroy()
            if resp == Gtk.ResponseType.YES:
                self.popdown()
                current_conv_id = self.get_current_conv_id_fn()
                current_deleted = False
                for conv_id in selected:
                    self.conversation_store.delete_conversation(conv_id)
                    if conv_id == current_conv_id:
                        current_deleted = True
                self._exit_edit_mode()
                if current_deleted:
                    self.on_clear_all_deleted_reset_fn()
                else:
                    self.refresh_dropdown()
            if self.on_dialog_hidden:
                self.on_dialog_hidden()

        dialog.connect("response", on_resp)
        if self.on_dialog_shown:
            self.on_dialog_shown()
        dialog.show_all()

    def refresh_dropdown(self, edit_mode: bool = False):
        for child in self.listbox.get_children():
            child.destroy()
            
        self._ai_history_switching = True
        summaries = self.get_sorted_conversations_fn()

        for s in summaries:
            sid = s.get("id", "")
            raw_title = s.get("title", "(untitled)")
            cleaned_title = _clean_history_title(raw_title)
            if len(cleaned_title) > 25:
                title = cleaned_title[:22] + "..."
            else:
                title = cleaned_title
            count = s.get("message_count", 0)
            label = f"{title} ({count}条)"
            
            row = Gtk.ListBoxRow.new()
            row.conversation_id = sid
            
            lbl = Gtk.Label.new(label)
            lbl.set_xalign(0)
            lbl.set_margin_end(8)
            lbl.set_margin_top(6)
            lbl.set_margin_bottom(6)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_max_width_chars(25)
            
            if edit_mode:
                hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
                check = Gtk.CheckButton.new()
                check.set_margin_start(6)
                check.set_margin_top(6)
                check.set_margin_bottom(6)
                is_selected = sid in self._ai_history_selected_ids
                check.set_active(is_selected)
                check.connect("toggled", lambda c, cid=sid: self._on_edit_check_toggled(c, cid))
                hbox.pack_start(check, False, False, 0)
                hbox.pack_start(lbl, True, True, 0)
                row.add(hbox)
                row.check_button = check
            else:
                lbl.set_margin_start(8)
                row.add(lbl)
            
            self.listbox.add(row)

        if summaries:
            self.history_btn.set_sensitive(True)
            self.history_btn.set_no_show_all(False)
            self.history_btn.show_all()
            self.update_history_btn_label()
            
            current_conv_id = self.get_current_conv_id_fn()
            if current_conv_id:
                for row in self.listbox.get_children():
                    if getattr(row, "conversation_id", None) == current_conv_id:
                        self.listbox.select_row(row)
                        break
        else:
            self.history_btn.set_sensitive(False)
            self.history_btn.set_no_show_all(True)
            self.history_btn.hide()

        self._ai_history_switching = False

    def update_history_btn_label(self, conv=None):
        current_conv_id = self.get_current_conv_id_fn()
        if not current_conv_id:
            self.history_btn_label.set_text("历史对话")
            return
        if conv:
            raw_title = conv.title if conv.title else "untitled"
            cleaned_title = _clean_history_title(raw_title)
            if len(cleaned_title) > 25:
                title = cleaned_title[:22] + "..."
            else:
                title = cleaned_title
            count = len(conv.messages) if conv.messages else 0
            label = f"{title} ({count}条)"
            self.history_btn_label.set_text(label)
            return

        active_label = "历史对话"
        for row in self.listbox.get_children():
            if getattr(row, "conversation_id", None) == current_conv_id:
                lbl = row.get_child()
                if isinstance(lbl, Gtk.Label):
                    active_label = lbl.get_text()
                break
        self.history_btn_label.set_text(active_label)
