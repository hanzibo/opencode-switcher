"""Recycle Bin dialog — extracted from clipboard_panel.py.

Self-contained GTK dialog for browsing, restoring, and permanently deleting
templates from the recycle bin.  Zero class coupling — all callbacks passed
as parameters.
"""

import time
from copy import deepcopy
from uuid import uuid4

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk

from clipboard_store import CategoryItem, CustomCategory


def show_recycle_bin_dialog(cat_store, parent_window, snippet_color,
                             on_dialog_shown=None,
                             on_dialog_hidden=None,
                             rebuild_category_list_cb=None,
                             rebuild_cb=None):
    """Build and show the Recycle Bin dialog.

    Parameters
    ----------
    cat_store : CategoryStore
        Store holding the recycle bin and category collections.
    parent_window : Gtk.Window
        Transient parent for the dialog window.
    snippet_color : Gdk.RGBA
        Colour used for the ``"From category:"`` label.
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
    dialog.set_title("Recycle Bin")
    dialog.set_modal(True)
    dialog.set_default_size(500, 400)
    dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
    dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
    dialog.set_resizable(True)
    dialog.set_transient_for(parent_window)

    # Transaction copies of the recycle bin and categories
    temp_recycle_bin = deepcopy(cat_store._recycle_bin)
    temp_categories = deepcopy(cat_store._categories)

    vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
    vbox.set_margin_top(12)
    vbox.set_margin_bottom(12)
    vbox.set_margin_start(12)
    vbox.set_margin_end(12)
    dialog.add(vbox)

    # Title Label
    title_lbl = Gtk.Label.new("Deleted Templates:")
    title_lbl.set_xalign(0)
    title_lbl.set_halign(Gtk.Align.START)
    vbox.pack_start(title_lbl, False, False, 0)

    # List Area
    scrolled = Gtk.ScrolledWindow.new()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_vexpand(True)
    listbox = Gtk.ListBox.new()
    listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
    scrolled.add(listbox)
    vbox.pack_start(scrolled, True, True, 0)

    # Middle bottom box: Restore button
    middle_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
    restore_btn = Gtk.Button.new_with_label("Restore")
    middle_box.pack_start(restore_btn, True, False, 0)
    vbox.pack_start(middle_box, False, False, 0)

    # Separator before bottom bar
    sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
    vbox.pack_start(sep, False, False, 0)

    # Bottom box: Permanently Delete (left) | Cancel & Confirm (right)
    bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)

    perm_delete_btn = Gtk.Button.new_with_label("Permanently Delete All")
    perm_delete_btn.get_style_context().add_class("destructive-action")
    bottom_box.pack_start(perm_delete_btn, False, False, 0)

    cancel_btn = Gtk.Button.new_with_label("Cancel")
    confirm_btn = Gtk.Button.new_with_label("Confirm")
    confirm_btn.get_style_context().add_class("suggested-action")

    bottom_box.pack_end(confirm_btn, False, False, 0)
    bottom_box.pack_end(cancel_btn, False, False, 0)
    vbox.pack_start(bottom_box, False, False, 0)

    def build_rows():
        for child in listbox.get_children():
            listbox.remove(child)
        for entry in temp_recycle_bin:
            row = Gtk.ListBoxRow.new()
            row.entry = entry

            hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)

            title = entry["item"]["title"]
            if not title:
                title = "(untitled)"
            title_lbl = Gtk.Label.new(title)
            title_lbl.set_halign(Gtk.Align.START)
            title_lbl.set_xalign(0)
            title_lbl.set_margin_start(12)
            title_lbl.set_margin_top(8)
            title_lbl.set_margin_bottom(8)

            cat_name = entry["original_cat_name"]
            cat_lbl = Gtk.Label.new(f"From category: {cat_name}")
            cat_lbl.set_halign(Gtk.Align.END)
            cat_lbl.set_xalign(1)
            cat_lbl.set_margin_end(12)
            cat_lbl.override_color(Gtk.StateFlags.NORMAL, snippet_color)

            hbox.pack_start(title_lbl, True, True, 0)
            hbox.pack_end(cat_lbl, False, False, 0)
            row.add(hbox)
            listbox.add(row)
        listbox.show_all()

    build_rows()

    # Restore action
    def on_restore_clicked(_btn):
        row = listbox.get_selected_row()
        if not row or not hasattr(row, "entry"):
            return
        entry = row.entry

        # Perform restore in temp
        temp_recycle_bin.remove(entry)

        orig_id = entry["original_cat_id"]
        orig_name = entry["original_cat_name"]
        item_data = entry["item"]
        item = CategoryItem(
            title=item_data["title"],
            text=item_data["text"],
            timestamp=item_data["timestamp"]
        )

        target_cat = None
        for c in temp_categories:
            if c.id == orig_id:
                target_cat = c
                break
        if not target_cat:
            for c in temp_categories:
                if c.name == orig_name:
                    target_cat = c
                    break
        if not target_cat:
            # To prevent naming conflicts in temp_categories
            new_cat_id = uuid4().hex[:12]
            target_cat = CustomCategory(
                id=new_cat_id,
                name=orig_name,
                items=[],
                pinned=False,
                created_at=int(time.time() * 1000)
            )
            temp_categories.append(target_cat)

        target_cat.items.append(item)
        build_rows()

    restore_btn.connect("clicked", on_restore_clicked)

    # Permanently Delete All action
    def on_perm_delete_clicked(_btn):
        if not temp_recycle_bin:
            return

        # Double confirmation dialog
        confirm = Gtk.MessageDialog(
            transient_for=dialog,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Permanently delete all templates?",
        )
        confirm.format_secondary_text(
            "This action cannot be undone and all templates in the "
            "Recycle Bin will be lost forever."
        )

        def on_confirm_response(dlg, resp):
            dlg.destroy()
            if resp == Gtk.ResponseType.YES:
                temp_recycle_bin.clear()
                build_rows()

        confirm.connect("response", on_confirm_response)
        confirm.show_all()

    perm_delete_btn.connect("clicked", on_perm_delete_clicked)

    # Cancel action
    def on_cancel_clicked(_btn):
        dialog.destroy()

    cancel_btn.connect("clicked", on_cancel_clicked)

    # Confirm action
    def on_confirm_clicked(_btn):
        cat_store._recycle_bin = temp_recycle_bin
        cat_store._categories = temp_categories
        cat_store._save()
        if rebuild_category_list_cb:
            rebuild_category_list_cb()
        if rebuild_cb:
            rebuild_cb()
        dialog.destroy()

    confirm_btn.connect("clicked", on_confirm_clicked)

    # Focus guards connection
    dialog.connect("show", lambda *_: on_dialog_shown and on_dialog_shown())
    dialog.connect("destroy", lambda *_: on_dialog_hidden and on_dialog_hidden())

    dialog.show_all()
