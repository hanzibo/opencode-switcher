from gi.repository import Gtk
from typing import Optional, Callable
from clipboard_store import CategoryItem, CategoryStore

def show_prompt_dialog(parent_window, create: bool, existing: Optional[CategoryItem],
                       active_category_id: str, cat_store: CategoryStore,
                       on_dialog_shown: Optional[Callable[[], None]],
                       on_dialog_hidden: Optional[Callable[[], None]],
                       load_cached_callback: Callable[[], None]):
    dialog = Gtk.Dialog(
        title="Create prompt" if create else "Edit prompt",
        transient_for=parent_window,
        modal=True,
    )
    dialog.set_default_size(520, 420)
    dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("_Save", Gtk.ResponseType.ACCEPT)
    dialog.set_default_response(Gtk.ResponseType.ACCEPT)

    content = dialog.get_content_area()
    content.set_spacing(8)
    content.set_margin_start(16)
    content.set_margin_end(16)
    content.set_margin_top(16)
    content.set_margin_bottom(16)

    content.add(Gtk.Label.new("Title:"))
    title_entry = Gtk.Entry.new()
    if existing:
        title_entry.set_text(existing.title)
    title_entry.set_activates_default(True)
    content.add(title_entry)

    content.add(Gtk.Label.new("Text:"))
    text_view = Gtk.TextView.new()
    text_view.set_wrap_mode(Gtk.WrapMode.WORD)
    sw = Gtk.ScrolledWindow.new()
    sw.set_min_content_height(240)
    sw.set_min_content_width(460)
    sw.add(text_view)
    content.pack_start(sw, True, True, 0)

    if existing:
        buf = text_view.get_buffer()
        buf.set_text(existing.text)

    if on_dialog_shown:
        on_dialog_shown()
    dialog.show_all()

    def on_response(dlg, response):
        title = title_entry.get_text().strip()
        buf = text_view.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, False).strip()
        dlg.destroy()
        if response != Gtk.ResponseType.ACCEPT or not title or not text:
            if on_dialog_hidden:
                on_dialog_hidden()
            return
        if create:
            cat_store.add_item(active_category_id, title, text)
        else:
            cat = cat_store.get(active_category_id)
            if cat:
                idx = next(
                    (i for i, ci in enumerate(cat.items)
                     if ci.timestamp == existing.timestamp),
                    None,
                )
                if idx is not None:
                    cat_store.update_item(active_category_id, idx, title, text)
        load_cached_callback()
        if on_dialog_hidden:
            on_dialog_hidden()

    dialog.connect("response", on_response)
