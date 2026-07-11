"""Dynamic Copy dialog — extracted from clipboard_panel.py.

Self-contained GTK dialog for template placeholder input and preview.
Zero class coupling — all dependencies and callbacks passed as parameters.
"""

import re
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, Pango, PangoCairo

# Regex to match placeholders: ${index[:prompt][=default]}
TEMPLATE_REGEX = re.compile(r"\$\{(\d+)(?::((?:[^}=]|\\:|\\=)+))?(?<!\\)(?:=([^}]*))?\}")


def _textview_draw_placeholder(widget, cr):
    """Draw placeholder text on a Gtk.TextView when its buffer is empty.

    Connect via: textview.connect_after("draw", _textview_draw_placeholder)
    Set placeholder via: textview.placeholder_text = "..."
    """
    buf = widget.get_buffer()
    if buf.get_char_count() == 0:
        placeholder = getattr(widget, "placeholder_text", "")
        if placeholder:
            text_window = widget.get_window(Gtk.TextWindowType.TEXT)
            if text_window and Gtk.cairo_should_draw_window(cr, text_window):
                cr.save()
                start_iter = buf.get_start_iter()
                rect = widget.get_iter_location(start_iter)
                left, top = widget.buffer_to_window_coords(Gtk.TextWindowType.TEXT, rect.x, rect.y)
                cr.translate(left, top)
                layout = widget.create_pango_layout(placeholder)
                context = widget.get_style_context()
                font_desc = context.get_property("font", Gtk.StateFlags.NORMAL)
                layout.set_font_description(font_desc)
                color = context.get_color(Gtk.StateFlags.NORMAL)
                cr.set_source_rgba(color.red, color.green, color.blue, 0.45)
                PangoCairo.show_layout(cr, layout)
                cr.restore()
    return False


def show_dynamic_copy_dialog(item, parent_window,
                              copy_to_clipboard_func,
                              on_copy_clipboard=None,
                              on_hide_request=None,
                              on_dialog_shown=None,
                              on_dialog_hidden=None,
                              unescape_template_field=None,
                              copy_to_ai_panel_func=None):
    """Build and show the Dynamic Copy template parameter dialog.

    Parameters
    ----------
    item : ClipboardItem
        The clipboard item whose text contains ``${index:prompt=default}`` placeholders.
    parent_window : Gtk.Window
        Transient parent for the dialog window.
    copy_to_clipboard_func : callable
        Function to perform actual OS clipboard write.
    on_copy_clipboard : callable or None
        Called with ``(text, item_hash)`` when user confirms copy.
    on_hide_request : callable or None
        Called after dialog is destroyed.
    on_dialog_shown : callable or None
        Called when dialog is shown (focus guard).
    on_dialog_hidden : callable or None
        Called when dialog is destroyed (focus guard).
    unescape_template_field : callable or None
        Function to unescape backslash-escaped colons/equals in template fields.
    copy_to_ai_panel_func : callable or None
        Function to insert text into the AI chat input box.
        Called with ``(text)`` when user clicks "Copy to AI Panel".
    """
    if unescape_template_field is None:
        unescape_template_field = lambda v: v.replace("\\:", ":").replace("\\=", "=")

    placeholders = {}
    defaults = {}
    for match in TEMPLATE_REGEX.finditer(item.text):
        num = int(match.group(1))
        prompt_text = match.group(2)
        default_text = match.group(3)
        if prompt_text:
            if num not in placeholders:
                placeholders[num] = unescape_template_field(prompt_text)
        if default_text:
            if num not in defaults:
                defaults[num] = unescape_template_field(default_text)

    matches = [m[0] for m in TEMPLATE_REGEX.findall(item.text)]
    if not matches:
        return

    nums = sorted(list(set(int(m) for m in matches)))

    dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
    dialog.get_style_context().add_class("custom-dialog")
    dialog.set_title("Dynamic Copy - {}".format(item.title if item.title else "Template"))
    dialog.set_modal(True)
    dialog.set_default_size(800, 500)
    dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
    dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
    dialog.set_resizable(True)
    dialog.set_transient_for(parent_window)

    # Main vertical container
    vbox_main = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
    vbox_main.set_margin_top(12)
    vbox_main.set_margin_bottom(12)
    vbox_main.set_margin_start(12)
    vbox_main.set_margin_end(12)
    dialog.add(vbox_main)

    # Left/Right columns container
    hbox_cols = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 12)
    vbox_main.pack_start(hbox_cols, True, True, 0)

    # --- Left Column: Parameter Input Panel ---
    vbox_input = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)

    lbl_input = Gtk.Label.new("Parameter Input:")
    lbl_input.set_xalign(0)
    vbox_input.pack_start(lbl_input, False, False, 0)

    notebook = Gtk.Notebook.new()
    notebook.set_show_border(False)
    vbox_input.pack_start(notebook, True, True, 0)
    hbox_cols.pack_start(vbox_input, True, True, 0)

    # --- Right Column: Real-time Preview & Edit Panel ---
    vbox_preview = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)

    lbl_preview = Gtk.Label.new("Real-time Preview:")
    lbl_preview.set_xalign(0)
    vbox_preview.pack_start(lbl_preview, False, False, 0)

    scrolled_preview = Gtk.ScrolledWindow.new()
    scrolled_preview.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scrolled_preview.set_vexpand(True)

    preview_tv = Gtk.TextView.new()
    preview_tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    preview_tv.set_left_margin(8)
    preview_tv.set_right_margin(8)
    preview_tv.set_top_margin(8)
    preview_tv.set_bottom_margin(8)
    preview_buffer = preview_tv.get_buffer()
    scrolled_preview.add(preview_tv)
    vbox_preview.pack_start(scrolled_preview, True, True, 0)

    chk_edit = Gtk.CheckButton.new_with_label("允许在预览框直接编辑")
    chk_edit.set_active(True)
    preview_tv.set_editable(True)

    def on_chk_edit_toggled(btn):
        preview_tv.set_editable(btn.get_active())

    chk_edit.connect("toggled", on_chk_edit_toggled)
    vbox_preview.pack_start(chk_edit, False, False, 0)
    hbox_cols.pack_start(vbox_preview, True, True, 0)

    # --- Data Binding and Key Event logic ---
    input_buffers = {}
    input_textviews = {}
    is_updating_preview = False

    def update_preview():
        nonlocal is_updating_preview
        if is_updating_preview:
            return
        is_updating_preview = True
        try:
            text = item.text
            for num, buf in input_buffers.items():
                start_iter = buf.get_start_iter()
                end_iter = buf.get_end_iter()
                val = buf.get_text(start_iter, end_iter, True)
                replacement = val or ""
                pattern = r"\$\{" + str(num) + r"(?:[:=][^}]+)?\}"
                text = re.sub(pattern, lambda m: replacement, text)
            preview_buffer.set_text(text)
        finally:
            is_updating_preview = False

    def on_key_press(widget, event, num_val):
        is_shift = (event.state & Gdk.ModifierType.SHIFT_MASK) != 0

        # Shift+Tab
        if event.keyval == Gdk.KEY_ISO_Left_Tab or (event.keyval == Gdk.KEY_Tab and is_shift):
            current_page = notebook.get_current_page()
            if current_page > 0:
                notebook.set_current_page(current_page - 1)
                prev_num = nums[current_page - 1]
                input_textviews[prev_num].grab_focus()
            return True

        # Tab
        if event.keyval == Gdk.KEY_Tab:
            current_page = notebook.get_current_page()
            n_pages = notebook.get_n_pages()
            if current_page < n_pages - 1:
                notebook.set_current_page(current_page + 1)
                next_num = nums[current_page + 1]
                input_textviews[next_num].grab_focus()
            else:
                confirm_btn.grab_focus()
            return True

        # Ctrl/Cmd+Enter
        is_enter = event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        has_modifier = (event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD4_MASK | Gdk.ModifierType.META_MASK)) != 0
        if is_enter and has_modifier:
            on_confirm(None)
            return True

        return False

    for num in nums:
        scr_in = Gtk.ScrolledWindow.new()
        scr_in.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scr_in.set_margin_top(6)
        scr_in.set_margin_bottom(6)
        scr_in.set_margin_start(6)
        scr_in.set_margin_end(6)

        tv_in = Gtk.TextView.new()
        tv_in.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv_in.set_left_margin(8)
        tv_in.set_right_margin(8)
        tv_in.set_top_margin(8)
        tv_in.set_bottom_margin(8)
        buf_in = tv_in.get_buffer()
        scr_in.add(tv_in)

        tab_lbl = Gtk.Label.new(f"${{{num}}}")
        notebook.append_page(scr_in, tab_lbl)

        input_buffers[num] = buf_in
        input_textviews[num] = tv_in

        # Set placeholder if defined
        if num in placeholders:
            tv_in.placeholder_text = placeholders[num]
            tv_in.connect_after("draw", _textview_draw_placeholder)

        # Set default text if defined
        if num in defaults:
            buf_in.set_text(defaults[num])

        # Select all text on focus
        def on_focus_in(widget, event):
            buf = widget.get_buffer()
            start, end = buf.get_bounds()
            buf.select_range(start, end)
            return False
        tv_in.connect("focus-in-event", on_focus_in)

        buf_in.connect("changed", lambda *_: update_preview())
        buf_in.connect("changed", lambda w, tv=tv_in: tv.queue_draw())
        tv_in.connect("key-press-event", on_key_press, num)

    def on_preview_key_press(widget, event):
        is_enter = event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        has_modifier = (event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD4_MASK | Gdk.ModifierType.META_MASK)) != 0
        if is_enter and has_modifier:
            on_confirm(None)
            return True
        return False

    preview_tv.connect("key-press-event", on_preview_key_press)

    # Initialize preview text
    update_preview()

    # Separator before buttons
    sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
    vbox_main.pack_start(sep, False, False, 0)

    # --- Bottom Buttons Box ---
    bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
    bottom_box.set_margin_top(8)
    bottom_box.set_margin_bottom(8)
    bottom_box.set_margin_end(12)

    cancel_btn = Gtk.Button.new_with_label("Cancel")
    cancel_btn.connect("clicked", lambda _: dialog.destroy())

    ai_btn = Gtk.Button.new_with_label("Copy to AI Panel")

    confirm_btn = Gtk.Button.new_with_label("Copy")
    confirm_btn.get_style_context().add_class("suggested-action")

    def _get_preview_text():
        start_iter = preview_buffer.get_start_iter()
        end_iter = preview_buffer.get_end_iter()
        return preview_buffer.get_text(start_iter, end_iter, True)

    def on_confirm(_btn):
        text = _get_preview_text()

        if on_copy_clipboard:
            on_copy_clipboard(text, None)
        copy_to_clipboard_func(text)

        dialog.destroy()

        if on_hide_request:
            on_hide_request()

    def on_copy_to_ai(_btn):
        text = _get_preview_text()
        if copy_to_ai_panel_func:
            copy_to_ai_panel_func(text)
        dialog.destroy()

    confirm_btn.connect("clicked", on_confirm)

    bottom_box.pack_end(confirm_btn, False, False, 0)
    bottom_box.pack_end(ai_btn, False, False, 0)
    bottom_box.pack_end(cancel_btn, False, False, 0)

    # Show AI button only if callback is provided
    if copy_to_ai_panel_func is not None:
        ai_btn.connect("clicked", on_copy_to_ai)
        ai_btn.show()
    else:
        ai_btn.set_no_show_all(True)
        ai_btn.hide()
    vbox_main.pack_start(bottom_box, False, False, 0)

    # Focus guards
    if on_dialog_shown:
        dialog.connect("show", lambda *_: on_dialog_shown())
    if on_dialog_hidden:
        dialog.connect("destroy", lambda *_: on_dialog_hidden())

    # Grab focus on the first input textview initially
    if nums:
        input_textviews[nums[0]].grab_focus()

    dialog.show_all()
