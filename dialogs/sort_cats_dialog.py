"""Sort Categories dialog — extracted from clipboard_panel.py.

Self-contained GTK dialog for drag-and-drop reordering of pinned and
normal categories.  Zero class coupling — all callbacks passed as parameters.
"""

from copy import deepcopy
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk


def show_sort_cats_dialog(cat_store, parent_window,
                           on_dialog_shown=None,
                           on_dialog_hidden=None,
                           rebuild_category_list_cb=None,
                           rebuild_cb=None):
    """Build and show the Sort Categories drag-and-drop dialog.

    Parameters
    ----------
    cat_store : CategoryStore
        Store holding all categories to be reordered.
    parent_window : Gtk.Window
        Transient parent for the dialog window.
    on_dialog_shown : callable or None
        Called when dialog is shown (focus guard).
    on_dialog_hidden : callable or None
        Called when dialog is destroyed (focus guard).
    rebuild_category_list_cb : callable or None
        Called after confirmation to rebuild the category sidebar list.
    rebuild_cb : callable or None
        Called after confirmation to fully rebuild the clipboard panel UI.
    """
    if on_dialog_shown:
        on_dialog_shown()

    dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
    dialog.get_style_context().add_class("custom-dialog")
    dialog.set_title("Sort Categories")
    dialog.set_modal(True)
    dialog.set_default_size(500, 400)
    dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
    dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
    dialog.set_resizable(True)
    dialog.set_transient_for(parent_window)

    # Exclude __clipboard__ as it's the system immutable category at top
    all_cats = [c for c in cat_store.get_all() if c.id != "__clipboard__"]
    temp_pinned = [c for c in all_cats if c.pinned]
    temp_normal = [c for c in all_cats if not c.pinned]

    vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
    vbox.set_margin_top(12)
    vbox.set_margin_bottom(12)
    vbox.set_margin_start(12)
    vbox.set_margin_end(12)
    dialog.add(vbox)

    # Tab layout using Notebook
    notebook = Gtk.Notebook.new()
    notebook.set_show_border(False)
    vbox.pack_start(notebook, True, True, 0)

    # Tab 1: Pinned Categories
    page1 = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
    scrolled1 = Gtk.ScrolledWindow.new()
    scrolled1.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled1.set_vexpand(True)
    listbox1 = Gtk.ListBox.new()
    listbox1.set_selection_mode(Gtk.SelectionMode.SINGLE)
    scrolled1.add(listbox1)
    page1.pack_start(scrolled1, True, True, 0)
    notebook.append_page(page1, Gtk.Label.new("Pinned Categories"))

    # Tab 2: Normal Categories
    page2 = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
    scrolled2 = Gtk.ScrolledWindow.new()
    scrolled2.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled2.set_vexpand(True)
    listbox2 = Gtk.ListBox.new()
    listbox2.set_selection_mode(Gtk.SelectionMode.SINGLE)
    scrolled2.add(listbox2)
    page2.pack_start(scrolled2, True, True, 0)
    notebook.append_page(page2, Gtk.Label.new("Normal Categories"))

    # ===== Drag & Drop setup =====
    target_entry = Gtk.TargetEntry.new("text/plain", Gtk.TargetFlags.SAME_APP, 0)

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
    listbox1.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)
    listbox2.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)

    def setup_dnd(lb, scr, items, rebuild_func):
        _current_hover_row = None
        _current_hover_dir = None

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

        def on_drag_motion(lb_widget, context, x, y, time):
            nonlocal _current_hover_row, _current_hover_dir
            vadj = scr.get_vadjustment()
            if vadj:
                visible_top = vadj.get_value()
                visible_height = vadj.get_page_size()
                clamped_y = max(visible_top, min(y, visible_top + visible_height))
            else:
                clamped_y = y

            row = lb_widget.get_row_at_y(clamped_y)
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

        def on_drag_leave(lb_widget, context, time):
            clear_hover_styling()

        def on_drag_data_received(lb_widget, context, x, y, sel_data, info, time):
            src_text = sel_data.get_text()
            if src_text is None:
                Gtk.drag_finish(context, False, False, time)
                return

            try:
                src_idx = int(src_text)
            except (ValueError, TypeError):
                Gtk.drag_finish(context, False, False, time)
                return

            vadj = scr.get_vadjustment()
            if vadj:
                visible_top = vadj.get_value()
                visible_height = vadj.get_page_size()
                clamped_y = max(visible_top, min(y, visible_top + visible_height))
            else:
                clamped_y = y

            row = lb_widget.get_row_at_y(clamped_y)
            clear_hover_styling()

            if row is None:
                dst_idx = len(items)
            else:
                alloc = row.get_allocation()
                row_y = clamped_y - alloc.y
                below = row_y > alloc.height / 2
                dst_idx = getattr(row, 'item_index', 0)
                if below:
                    dst_idx += 1

            if src_idx == dst_idx or src_idx == dst_idx - 1:
                Gtk.drag_finish(context, False, False, time)
                return

            item = items.pop(src_idx)
            if dst_idx > src_idx:
                dst_idx -= 1
            items.insert(dst_idx, item)

            rebuild_func()

            children = lb_widget.get_children()
            if 0 <= dst_idx < len(children):
                lb_widget.select_row(children[dst_idx])

            Gtk.drag_finish(context, True, False, time)

        lb.drag_dest_set(Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP,
                         [target_entry], Gdk.DragAction.MOVE)
        lb.connect("drag-motion", on_drag_motion)
        lb.connect("drag-leave", on_drag_leave)
        lb.connect("drag-data-received", on_drag_data_received)

        return on_drag_data_get, on_drag_end

    # ===== Pinned Categories build_rows =====
    def build_pinned():
        for child in listbox1.get_children():
            listbox1.remove(child)
        for idx, cat in enumerate(temp_pinned):
            row = Gtk.ListBoxRow.new()
            row.item_index = idx
            row.get_style_context().add_class("sort-row")
            row.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)
            row.set_size_request(-1, 36)

            evbox = Gtk.EventBox.new()
            evbox.item_index = idx

            lbl = Gtk.Label.new(cat.name if cat.name else "(untitled)")
            lbl.set_xalign(0)
            lbl.set_margin_start(16)
            lbl.set_margin_top(6)
            lbl.set_margin_bottom(6)
            evbox.add(lbl)
            row.add(evbox)

            evbox.drag_source_set(Gdk.ModifierType.BUTTON1_MASK,
                                  [target_entry], Gdk.DragAction.MOVE)
            evbox.connect("drag-data-get", drag_get1)
            evbox.connect("drag-end", drag_end1)

            listbox1.add(row)
        listbox1.show_all()

    # ===== Normal Categories build_rows =====
    def build_normal():
        for child in listbox2.get_children():
            listbox2.remove(child)
        for idx, cat in enumerate(temp_normal):
            row = Gtk.ListBoxRow.new()
            row.item_index = idx
            row.get_style_context().add_class("sort-row")
            row.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)
            row.set_size_request(-1, 36)

            evbox = Gtk.EventBox.new()
            evbox.item_index = idx

            lbl = Gtk.Label.new(cat.name if cat.name else "(untitled)")
            lbl.set_xalign(0)
            lbl.set_margin_start(16)
            lbl.set_margin_top(6)
            lbl.set_margin_bottom(6)
            evbox.add(lbl)
            row.add(evbox)

            evbox.drag_source_set(Gdk.ModifierType.BUTTON1_MASK,
                                  [target_entry], Gdk.DragAction.MOVE)
            evbox.connect("drag-data-get", drag_get2)
            evbox.connect("drag-end", drag_end2)

            listbox2.add(row)
        listbox2.show_all()

    # Connect drag handlers
    drag_get1, drag_end1 = setup_dnd(listbox1, scrolled1,
                                      temp_pinned, build_pinned)
    drag_get2, drag_end2 = setup_dnd(listbox2, scrolled2,
                                      temp_normal, build_normal)

    # Build initially
    build_pinned()
    build_normal()

    # Separator before bottom bar
    sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
    vbox.pack_start(sep, False, False, 0)

    # Bottom buttons box (right-aligned)
    bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
    bottom_box.set_margin_top(8)
    bottom_box.set_margin_bottom(8)
    bottom_box.set_margin_end(12)

    cancel_btn = Gtk.Button.new_with_label("Cancel")
    cancel_btn.connect("clicked", lambda _: dialog.destroy())

    confirm_btn = Gtk.Button.new_with_label("Confirm")
    confirm_btn.get_style_context().add_class("suggested-action")

    def on_confirm(_btn):
        # Combine the ordered pinned and normal categories back
        cat_store.reorder_categories(temp_pinned + temp_normal)
        if rebuild_category_list_cb:
            rebuild_category_list_cb()
        if rebuild_cb:
            rebuild_cb()
        dialog.destroy()

    confirm_btn.connect("clicked", on_confirm)

    bottom_box.pack_end(confirm_btn, False, False, 0)
    bottom_box.pack_end(cancel_btn, False, False, 0)
    vbox.pack_start(bottom_box, False, False, 0)

    # Focus guards connection
    dialog.connect("show", lambda *_: on_dialog_shown and on_dialog_shown())
    dialog.connect("destroy", lambda *_: on_dialog_hidden and on_dialog_hidden())

    dialog.show_all()
