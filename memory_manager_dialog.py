"""记忆管理对话框 — 对已保存的记忆进行增删改查。"""

from gi.repository import Gtk, GLib
from typing import Optional, Callable
from clipboard_store import MemStore


def show_memory_manager_dialog(parent,
                               on_dialog_shown: Optional[Callable[[], None]] = None,
                               on_dialog_hidden: Optional[Callable[[], None]] = None):
    store = MemStore()

    dialog = Gtk.Dialog(
        title="🗄️ 管理记忆",
        transient_for=parent,
        modal=True,
    )
    dialog.set_default_size(560, 480)

    content = dialog.get_content_area()
    content.set_spacing(8)
    content.set_margin_start(12)
    content.set_margin_end(12)
    content.set_margin_top(12)
    content.set_margin_bottom(12)

    # ── Search bar ──
    search_entry = Gtk.SearchEntry.new()
    search_entry.set_placeholder_text("🔍 搜索记忆（BM25 语义匹配）...")
    content.pack_start(search_entry, False, False, 0)

    # ── Memory list ──
    listbox = Gtk.ListBox.new()
    sw = Gtk.ScrolledWindow.new()
    sw.set_min_content_height(300)
    sw.add(listbox)
    content.pack_start(sw, True, True, 0)

    # ── Action buttons ──
    btn_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
    add_btn = Gtk.Button.new_with_label("➕ 新建")
    edit_btn = Gtk.Button.new_with_label("✏️ 编辑")
    del_btn = Gtk.Button.new_with_label("🗑️ 删除")
    edit_btn.set_sensitive(False)
    del_btn.set_sensitive(False)
    btn_box.pack_start(add_btn, False, False, 0)
    btn_box.pack_start(edit_btn, False, False, 0)
    btn_box.pack_start(del_btn, False, False, 0)
    close_btn = Gtk.Button.new_with_label("关闭")
    btn_box.pack_end(close_btn, False, False, 0)
    content.pack_start(btn_box, False, False, 0)

    # ── Functions ──

    def refresh_list(filter_text: str = ""):
        # Clear existing rows
        for row in listbox.get_children():
            listbox.remove(row)
        # Load data
        if filter_text:
            items = store.search(filter_text)
        else:
            items = store.list_recent(100)
        if not items:
            lbl = Gtk.Label.new("（暂无已保存的记忆）")
            row = Gtk.ListBoxRow.new()
            row.add(lbl)
            row.set_sensitive(False)
            listbox.add(row)
        else:
            for item in items:
                display = f"{item.key}: {item.value[:80]}"
                if len(item.value) > 80:
                    display += "..."
                lbl = Gtk.Label.new(display)
                lbl.set_xalign(0)
                lbl.set_halign(Gtk.Align.START)
                lbl.set_margin_start(8)
                lbl.set_margin_end(8)
                lbl.set_margin_top(4)
                lbl.set_margin_bottom(4)
                row = Gtk.ListBoxRow.new()
                row.add(lbl)
                row._key = item.key
                row._value = item.value
                listbox.add(row)
        listbox.show_all()

    def update_btns(row):
        has_sel = row is not None and hasattr(row, '_key')
        edit_btn.set_sensitive(has_sel)
        del_btn.set_sensitive(has_sel)

    def on_add_clicked(*_):
        _show_edit_dialog(dialog, store, None, None, refresh_list)

    def on_edit_clicked(*_):
        row = listbox.get_selected_row()
        if row and hasattr(row, '_key'):
            _show_edit_dialog(dialog, store, row._key, row._value, refresh_list)

    def on_delete_clicked(*_):
        row = listbox.get_selected_row()
        if not row or not hasattr(row, '_key'):
            return
        key = row._key
        confirm = Gtk.MessageDialog(
            transient_for=dialog,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f'删除记忆「{key}」？',
        )
        confirm.format_secondary_text("此操作不可撤销。")
        confirm.connect("response", lambda dlg, resp: (
            dlg.destroy(),
            store.delete(key),
            store.save(),
            refresh_list(search_entry.get_text().strip()),
        ) if resp == Gtk.ResponseType.YES else dlg.destroy())
        confirm.show_all()

    # ── Signal connections ──
    search_entry.connect("search-changed", lambda e: refresh_list(e.get_text().strip()))
    listbox.connect("row-selected", lambda box, row: update_btns(row))
    add_btn.connect("clicked", on_add_clicked)
    edit_btn.connect("clicked", on_edit_clicked)
    del_btn.connect("clicked", on_delete_clicked)
    close_btn.connect("clicked", lambda *_: dialog.destroy())

    # ── Show ──
    if on_dialog_shown:
        on_dialog_shown()
    dialog.show_all()
    refresh_list()

    dialog.connect("destroy", lambda *_: on_dialog_hidden() if on_dialog_hidden else None)
    dialog.run()
    dialog.destroy()


def _show_edit_dialog(parent, store: MemStore, key: Optional[str], value: Optional[str],
                      on_saved: Callable[[], None]):
    is_new = key is None
    dlg = Gtk.Dialog(
        title="新建记忆" if is_new else "编辑记忆",
        transient_for=parent,
        modal=True,
    )
    dlg.set_default_size(420, 250)
    dlg.add_button("_Cancel", Gtk.ResponseType.CANCEL)
    dlg.add_button("_Save", Gtk.ResponseType.ACCEPT)

    c = dlg.get_content_area()
    c.set_spacing(8)
    c.set_margin_start(12)
    c.set_margin_end(12)
    c.set_margin_top(12)
    c.set_margin_bottom(12)

    c.add(Gtk.Label.new("键名 (key):"))
    key_entry = Gtk.Entry.new()
    if key:
        key_entry.set_text(key)
        key_entry.set_sensitive(False)  # 编辑时不可改键名
    key_entry.set_activates_default(True)
    c.add(key_entry)

    c.add(Gtk.Label.new("内容 (value):"))
    buf = Gtk.TextBuffer.new()
    if value:
        buf.set_text(value)
    tv = Gtk.TextView.new_with_buffer(buf)
    tv.set_wrap_mode(Gtk.WrapMode.WORD)
    sw = Gtk.ScrolledWindow.new()
    sw.set_min_content_height(120)
    sw.add(tv)
    c.pack_start(sw, True, True, 0)

    dlg.show_all()

    def on_response(d, resp):
        k = key_entry.get_text().strip()
        start, end = buf.get_bounds()
        v = buf.get_text(start, end, False).strip()
        d.destroy()
        if resp != Gtk.ResponseType.ACCEPT or not k or not v:
            return
        store.put(k, v)
        store.save()
        on_saved()

    dlg.connect("response", on_response)
