#!/usr/bin/env python3
"""Standalone test for GTK3 ListBox drag-and-drop reordering.
Uses Gtk.EventBox as drag source and Gtk.ListBox as drag destination.
Visual feedback is provided via CSS border colors on the hovered row.
Borders are 3px solid transparent by default to ensure row allocations
are 100% static and do not trigger queue_resize() / layout invalidations.
"""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib

# 20 items to force scrolling
INITIAL_ITEMS = [f"Item {chr(65+i)}" for i in range(20)]


class SortDialogTest:
    def __init__(self):
        self.items = list(INITIAL_ITEMS)
        self.initial_order = list(INITIAL_ITEMS)

        win = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        win.set_title("DnD Test - Drag items to reorder (5s)")
        win.set_default_size(400, 350)
        win.set_position(Gtk.WindowPosition.CENTER)

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        win.add(vbox)

        lbl = Gtk.Label.new("Drag items to reorder. Order will be checked in 5 seconds.")
        lbl.set_margin_top(8)
        lbl.set_margin_bottom(8)
        vbox.pack_start(lbl, False, False, 0)

        sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep, False, False, 0)

        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        listbox = Gtk.ListBox.new()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.add(listbox)
        vbox.pack_start(scrolled, True, True, 0)

        # Use "text/plain" target
        target_entry = Gtk.TargetEntry.new("text/plain", Gtk.TargetFlags.SAME_APP, 0)

        # Apply CSS for visual feedback borders with constant size
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .sort-row {
                border-top: 3px solid transparent;
                border-bottom: 3px solid transparent;
            }
            .sort-row.drag-hover-top {
                border-top-color: #3584e4;
            }
            .sort-row.drag-hover-bottom {
                border-bottom-color: #3584e4;
            }
        """)
        listbox.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)

        # Track currently highlighted row
        _current_hover_row = None
        _current_hover_dir = None

        # Set DnD DEST on Gtk.ListBox itself (omit HIGHLIGHT to prevent green border)
        listbox.drag_dest_set(Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP, [target_entry], Gdk.DragAction.MOVE)

        def clear_hover_styling():
            nonlocal _current_hover_row, _current_hover_dir
            if _current_hover_row:
                try:
                    ctx = _current_hover_row.get_style_context()
                    ctx.remove_class("drag-hover-top")
                    ctx.remove_class("drag-hover-bottom")
                except Exception:
                    pass
                _current_hover_row = None
                _current_hover_dir = None

        def on_drag_data_get(widget, context, sel_data, info, time):
            sel_data.set_text(str(widget.item_index), -1)

        def on_drag_end(widget, context):
            clear_hover_styling()

        def on_drag_motion(lb, context, x, y, time):
            nonlocal _current_hover_row, _current_hover_dir
            vadj = scrolled.get_vadjustment()
            if vadj:
                visible_top = vadj.get_value()
                visible_height = vadj.get_page_size()
                clamped_y = max(visible_top, min(y, visible_top + visible_height))
            else:
                clamped_y = y

            row = lb.get_row_at_y(clamped_y)
            if row is None:
                clear_hover_styling()
                Gdk.drag_status(context, Gdk.DragAction.MOVE, time)
                return True

            alloc = row.get_allocation()
            row_y = clamped_y - alloc.y
            below = row_y > alloc.height / 2
            direction = 'bottom' if below else 'top'

            if _current_hover_row != row or _current_hover_dir != direction:
                clear_hover_styling()
                _current_hover_row = row
                _current_hover_dir = direction
                ctx = row.get_style_context()
                if direction == 'top':
                    ctx.add_class("drag-hover-top")
                else:
                    ctx.add_class("drag-hover-bottom")

            Gdk.drag_status(context, Gdk.DragAction.MOVE, time)
            return True

        def on_drag_leave(lb, context, time):
            clear_hover_styling()

        def on_drag_data_received(lb, context, x, y, sel_data, info, time):
            target_name = sel_data.get_target().name()
            print(f"[debug] on_drag_data_received: x={x}, y={y}, target={target_name}")
            src_text = sel_data.get_text()
            print(f"[debug] src_text={src_text}")
            if src_text is None:
                Gtk.drag_finish(context, False, False, time)
                return
            try:
                src_idx = int(src_text)
            except (ValueError, TypeError):
                Gtk.drag_finish(context, False, False, time)
                return

            # Clamp y to the visible viewport bounds of the ScrolledWindow
            vadj = scrolled.get_vadjustment()
            if vadj:
                visible_top = vadj.get_value()
                visible_height = vadj.get_page_size()
                clamped_y = max(visible_top, min(y, visible_top + visible_height))
                print(f"[debug] scrolled: visible_top={visible_top}, visible_height={visible_height}, y={y} -> clamped_y={clamped_y}")
            else:
                clamped_y = y

            row = lb.get_row_at_y(clamped_y)
            clear_hover_styling()

            if row is None:
                dst_idx = len(self.items)
                print(f"[debug] Row is None, fallback to bottom, resolved index={dst_idx}")
            else:
                alloc = row.get_allocation()
                row_y = clamped_y - alloc.y
                below = row_y > alloc.height / 2
                dst_idx = getattr(row, 'item_index', 0)
                if below:
                    dst_idx += 1
                print(f"[debug] Dropped on row {row.item_index}, below={below}, resolved index={dst_idx}")

            if src_idx == dst_idx or src_idx == dst_idx - 1:
                Gtk.drag_finish(context, False, False, time)
                return

            item = self.items.pop(src_idx)
            if dst_idx > src_idx:
                dst_idx -= 1
            self.items.insert(dst_idx, item)

            build_rows()
            Gtk.drag_finish(context, True, False, time)

        listbox.connect("drag-motion", on_drag_motion)
        listbox.connect("drag-leave", on_drag_leave)
        listbox.connect("drag-data-received", on_drag_data_received)

        def build_rows():
            for child in listbox.get_children():
                listbox.remove(child)
            for idx, item in enumerate(self.items):
                row = Gtk.ListBoxRow.new()
                row.item_index = idx
                row.get_style_context().add_class("sort-row")
                row.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)

                # EventBox as drag source (has own GdkWindow)
                evbox = Gtk.EventBox.new()
                evbox.item_index = idx

                lbl = Gtk.Label.new(item)
                lbl.set_xalign(0)
                lbl.set_margin_start(16)
                lbl.set_margin_top(8)
                lbl.set_margin_bottom(8)
                evbox.add(lbl)
                row.add(evbox)

                # DnD SOURCE on EventBox
                evbox.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [target_entry], Gdk.DragAction.MOVE)
                evbox.connect("drag-data-get", on_drag_data_get)
                evbox.connect("drag-end", on_drag_end)

                listbox.add(row)
            listbox.show_all()

        build_rows()

        # Bottom buttons
        bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        bottom_box.set_margin_top(8)
        bottom_box.set_margin_bottom(8)
        bottom_box.set_margin_end(12)

        def on_confirm(_btn):
            print("=" * 50)
            print("DnD TEST RESULT (manual)")
            print("=" * 50)
            print("Initial:", self.initial_order)
            print("Final:  ", self.items)
            if self.items != self.initial_order:
                print(">>> ORDER CHANGED <<<  DnD is WORKING")
            else:
                print(">>> ORDER UNCHANGED <<<")
            win.destroy()
            Gtk.main_quit()

        confirm_btn = Gtk.Button.new_with_label("Confirm")
        confirm_btn.connect("clicked", on_confirm)
        bottom_box.pack_end(confirm_btn, False, False, 0)
        vbox.pack_start(bottom_box, False, False, 0)

        win.connect("destroy", Gtk.main_quit)
        win.show_all()

        # Timeout to auto-exit in case user does not interact
        GLib.timeout_add_seconds(15, lambda: win.destroy() or Gtk.main_quit())


if __name__ == "__main__":
    app = SortDialogTest()
    Gtk.main()
