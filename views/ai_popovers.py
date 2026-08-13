import logging
import re
from gi.repository import Gtk, Gdk, GLib, Pango
from typing import Optional, Callable, Set, List, Dict, Tuple, Any

logger = logging.getLogger(__name__)

class AICommandPopover(Gtk.Popover):
    def __init__(self, relative_to_entry, command_list: List[Tuple[str, str]], conversation_id_fn: Optional[Callable[[], Optional[str]]] = None):
        super().__init__(relative_to=relative_to_entry)
        self.entry = relative_to_entry
        self.command_list = command_list
        self.conversation_id_fn = conversation_id_fn
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
        self._scrolled_window = cmd_sw

        self.listbox = Gtk.ListBox.new()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(False)
        self.listbox.get_style_context().add_class("command-autocomplete-list")
        self.listbox.connect("row-activated", self._on_cmd_row_activated)

        cmd_sw.add(self.listbox)
        self.add(cmd_sw)
        self.connect("closed", self._on_cmd_popover_closed)
        self.connect("key-press-event", self._on_cmd_popover_key_press)

    def scroll_to_row(self, row):
        """Scroll the ScrolledWindow to make *row* visible."""
        if row is None:
            return
        adj = self._scrolled_window.get_vadjustment()
        alloc = row.get_allocation()
        page = adj.get_page_size()
        cur = adj.get_value()
        if alloc.y < cur:
            adj.set_value(alloc.y)
        elif alloc.y + alloc.height > cur + page:
            adj.set_value(alloc.y + alloc.height - page)

    def _on_cmd_popover_closed(self, _popover):
        self._ai_cmd_popover_visible = False

    def is_visible(self) -> bool:
        return self._ai_cmd_popover_visible

    def rebuild(self, prefix: str):
        if self._ai_cmd_suppress_rebuild:
            return
        search = prefix.lstrip("/")
        matches: List[Tuple[str, str]] = []

        # 动态技能补全支持
        if search == "skill" or search.startswith("skill") or search.startswith("skill:") or search.startswith("skill "):
            try:
                from stores.skill_store import SkillStore
                import tool_registry
                conv_id = self.conversation_id_fn() if self.conversation_id_fn else None
                cwd = tool_registry.get_bash_cwd(session_key=conv_id)
                skills = SkillStore().get_skills(cwd=cwd)
                filter_term = ""
                if search in ("skill", "skill ", "skill:"):
                    filter_term = ""
                elif ":" in search:
                    filter_term = search.split(":", 1)[1].strip()
                elif search.startswith("skill "):
                    filter_term = search[6:].strip()

                for sk in skills:
                    if not filter_term or filter_term.lower() in sk.name.lower() or filter_term.lower() in sk.description.lower():
                        matches.append((f"skill:{sk.name}", f"[u] {sk.description}"))
            except Exception as e:
                logger.debug(f"Skill autocomplete rebuild error: {e}")

        if not matches:
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
            hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 12)
            
            lbl_cmd = Gtk.Label.new(cmd)
            lbl_cmd.set_xalign(0)
            lbl_cmd.set_margin_start(8)
            lbl_cmd.set_margin_top(6)
            lbl_cmd.set_margin_bottom(6)

            lbl_desc = Gtk.Label.new(desc)
            lbl_desc.set_xalign(0)
            lbl_desc.set_hexpand(True)
            lbl_desc.set_ellipsize(Pango.EllipsizeMode.END)
            lbl_desc.set_opacity(0.65)
            lbl_desc.set_margin_end(8)
            lbl_desc.set_margin_top(6)
            lbl_desc.set_margin_bottom(6)

            hbox.pack_start(lbl_cmd, False, False, 0)
            hbox.pack_start(lbl_desc, True, True, 0)
            row.add(hbox)
            row._cmd_command = cmd
            self.listbox.add(row)

        self.listbox.show_all()
        first = self.listbox.get_row_at_index(0)
        if first:
            self.listbox.select_row(first)
        self.listbox.handler_unblock_by_func(self._on_cmd_row_activated)

        child = self.get_child()
        if child:
            entry_width = self.entry.get_allocated_width()
            min_width = 180
            child.set_size_request(max(entry_width, min_width), -1)
            child.show_all()
        self.popup()
        self.show_all()
        self._ai_cmd_popover_visible = True

    def _on_cmd_popover_key_press(self, _popover, event):
        keyname = Gdk.keyval_name(event.keyval)
        state = event.state
        is_ctrl = (state & Gdk.ModifierType.CONTROL_MASK) != 0
        is_alt = (state & Gdk.ModifierType.MOD1_MASK) != 0

        if keyname in ("Up", "KP_Up"):
            current = self.listbox.get_selected_row()
            if current:
                idx = current.get_index()
                if idx > 0:
                    above = self.listbox.get_row_at_index(idx - 1)
                    if above:
                        self.listbox.select_row(above)
                        self.scroll_to_row(above)
            return True

        if keyname in ("Down", "KP_Down"):
            current = self.listbox.get_selected_row()
            if current:
                idx = current.get_index()
                below = self.listbox.get_row_at_index(idx + 1)
                if below:
                    self.listbox.select_row(below)
                    self.scroll_to_row(below)
            else:
                first = self.listbox.get_row_at_index(0)
                if first:
                    self.listbox.select_row(first)
                    self.scroll_to_row(first)
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

        if not command.startswith("/"):
            command = "/" + command

        buf = self.entry.get_buffer()
        if command == "/skill":
            # 补全 /skill 时，自动追加 ':' 并由 GLib.idle_add 延迟重新构建，确保 GTK 事件处理完毕后 Popover 稳定开启
            self._ai_cmd_suppress_rebuild = False
            self._ai_cmd_popover_visible = False
            buf.set_text("/skill:")
            end = buf.get_end_iter()
            buf.place_cursor(end)
            GLib.idle_add(self.rebuild, "/skill:")
            return

        self._ai_cmd_suppress_rebuild = True
        buf.set_text(command + " ")
        end = buf.get_end_iter()
        buf.place_cursor(end)
        self._ai_cmd_suppress_rebuild = False
        self.dismiss()
