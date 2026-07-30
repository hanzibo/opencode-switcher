"""Sort Items dialog — extracted from clipboard_panel.py.

Self-contained GTK dialog for drag-and-drop reordering of clipboard items
within a single category.  Zero class coupling — all callbacks passed as parameters.
"""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk


def show_sort_dialog(cat_store, category_id, parent_window,
                      on_dialog_shown=None,
                      on_dialog_hidden=None,
                      rebuild_cb=None):
    """Build and show the Sort Items drag-and-drop dialog.

    Parameters
    ----------
    cat_store : CategoryStore
        Store holding the category whose items should be reordered.
    category_id : str
        ID of the category whose items to sort.
    parent_window : Gtk.Window
        Transient parent for the dialog window.
    on_dialog_shown : callable or None
        Called when dialog is shown (focus guard).
    on_dialog_hidden : callable or None
        Called when dialog is destroyed (focus guard).
    rebuild_cb : callable or None
        Called after confirmation to fully rebuild the clipboard panel UI.
    """
    cat = cat_store.get(category_id)
    if not cat or len(cat.items) <= 1:
        return

    items = list(cat.items)  # local working copy

    dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
    dialog.get_style_context().add_class("custom-dialog")
    dialog.set_title("Sort: {}".format(cat.name))
    dialog.set_modal(True)
    dialog.set_default_size(500, 400)
    dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
    dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
    dialog.set_resizable(True)
    dialog.set_transient_for(parent_window)

    # Main vertical box
    vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
    dialog.add(vbox)

    title_label = Gtk.Label.new("Drag items to reorder: {}".format(cat.name))
    title_label.set_xalign(0)
    title_label.set_margin_start(12)
    title_label.set_margin_top(8)
    title_label.set_margin_bottom(8)
    vbox.pack_start(title_label, False, False, 0)

    # Separator after top bar
    sep1 = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
    vbox.pack_start(sep1, False, False, 0)

    # ===== Scrollable ListBox =====
    scrolled = Gtk.ScrolledWindow.new()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_vexpand(True)
    listbox = Gtk.ListBox.new()
    listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
    scrolled.add(listbox)
    vbox.pack_start(scrolled, True, True, 0)

    # ===== Drag & Drop setup =====
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
    listbox.drag_dest_set(Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP,
                          [target_entry], Gdk.DragAction.MOVE)

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
        src_text = sel_data.get_text()
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
        else:
            clamped_y = y

        row = lb.get_row_at_y(clamped_y)
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

        build_rows()

        children = lb.get_children()
        if 0 <= dst_idx < len(children):
            lb.select_row(children[dst_idx])

        Gtk.drag_finish(context, True, False, time)

    listbox.connect("drag-motion", on_drag_motion)
    listbox.connect("drag-leave", on_drag_leave)
    listbox.connect("drag-data-received", on_drag_data_received)

    # ===== Fill listbox with rows =====
    def build_rows():
        for child in listbox.get_children():
            listbox.remove(child)
        for idx, item in enumerate(items):
            row = Gtk.ListBoxRow.new()
            row.item_index = idx
            row.get_style_context().add_class("sort-row")
            row.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)
            row.set_size_request(-1, 36)

            evbox = Gtk.EventBox.new()
            evbox.item_index = idx

            lbl = Gtk.Label.new(item.title if item.title else "(untitled)")
            lbl.set_xalign(0)
            lbl.set_margin_start(16)
            lbl.set_margin_top(6)
            lbl.set_margin_bottom(6)
            evbox.add(lbl)
            row.add(evbox)

            evbox.drag_source_set(Gdk.ModifierType.BUTTON1_MASK,
                                  [target_entry], Gdk.DragAction.MOVE)
            evbox.connect("drag-data-get", on_drag_data_get)
            evbox.connect("drag-end", on_drag_end)

            listbox.add(row)
        listbox.show_all()

    # ===== Bottom buttons =====
    bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
    bottom_box.set_margin_top(8)
    bottom_box.set_margin_bottom(8)
    bottom_box.set_margin_end(12)

    cancel_btn = Gtk.Button.new_with_label("Cancel")
    cancel_btn.connect("clicked", lambda _: dialog.destroy())

    confirm_btn = Gtk.Button.new_with_label("Confirm")
    confirm_btn.get_style_context().add_class("suggested-action")

    def on_confirm(_btn):
        cat_store.reorder_items(category_id, items)
        if rebuild_cb:
            rebuild_cb()
        dialog.destroy()

    confirm_btn.connect("clicked", on_confirm)

    bottom_box.pack_end(confirm_btn, False, False, 0)
    bottom_box.pack_end(cancel_btn, False, False, 0)
    vbox.pack_start(bottom_box, False, False, 0)

    sep2 = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
    vbox.pack_start(sep2, False, False, 0)
    # Move bottom_box after sep2 in the vbox order
    vbox.reorder_child(bottom_box, -1)

    # ===== Build rows initially =====
    build_rows()

    # ===== Wire focus guards =====
    dialog.connect("show", lambda *_: on_dialog_shown and on_dialog_shown())
    dialog.connect("destroy", lambda *_: on_dialog_hidden and on_dialog_hidden())

    dialog.show_all()
