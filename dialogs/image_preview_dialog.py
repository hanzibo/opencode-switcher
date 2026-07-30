"""Image Lightbox Preview Dialog — standalone GTK dialog with WebKit2 WebView.

Provides full-screen/modal image zooming and panning (scroll to zoom,
drag to pan, double click to reset, click background / Esc to exit),
reusing the Lightbox implementation from html_templates/chat.js.
"""

import os
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    try:
        gi.require_version("WebKit2", "4.0")
    except ValueError:
        pass

from gi.repository import Gtk, Gdk, GLib
try:
    from gi.repository import WebKit2
except ImportError:
    WebKit2 = None

from ai_engine.ai_html_template import _CHAT_CSS, _CHAT_JS, get_shared_web_context


def show_image_preview_dialog(
    image_path: str,
    parent_window: Gtk.Window = None,
    on_dialog_shown=None,
    on_dialog_hidden=None,
):
    """Show the standalone image lightbox preview dialog.

    Parameters
    ----------
    image_path : str
        Absolute path to the image file to view.
    parent_window : Gtk.Window or None
        Transient parent window for modal behavior.
    on_dialog_shown : callable or None
        Focus-guard callback called when dialog is presented.
    on_dialog_hidden : callable or None
        Focus-guard callback called when dialog is destroyed.
    """
    if not image_path or not os.path.isfile(image_path):
        return

    image_url = f"file://{os.path.abspath(image_path)}"

    if on_dialog_shown:
        on_dialog_shown()

    dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
    dialog.set_title("图片预览")
    dialog.set_modal(True)
    dialog.set_default_size(960, 720)
    dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
    dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
    dialog.set_resizable(True)
    if parent_window and isinstance(parent_window, Gtk.Window):
        dialog.set_transient_for(parent_window)

    # Dark styling for dialog background
    dialog.get_style_context().add_class("image-preview-dialog")
    provider = Gtk.CssProvider.new()
    provider.load_from_data(b"""
        .image-preview-dialog {
            background-color: #0b0b12;
        }
    """)
    dialog.get_style_context().add_provider(
        provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    if WebKit2 is None:
        lbl = Gtk.Label.new("WebKit2GTK is missing, cannot preview image.")
        dialog.add(lbl)
        dialog.show_all()
        return

    web_context = get_shared_web_context()
    webview = WebKit2.WebView.new_with_context(web_context) if web_context else WebKit2.WebView.new()
    settings = webview.get_settings()
    settings.enable_webgl = False
    settings.enable_html5_database = False
    settings.enable_html5_local_storage = False

    dialog.add(webview)

    # Lightbox HTML page
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        html, body {{
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100%;
            background-color: #0b0b12;
            overflow: hidden;
            user-select: none;
            -webkit-user-select: none;
        }}
        {_CHAT_CSS}
        .lightbox-overlay {{
            display: flex !important;
            opacity: 1 !important;
            background: rgba(11, 11, 18, 0.92) !important;
        }}
    </style>
    <script>{_CHAT_JS}</script>
</head>
<body class="dark">
    <div id="content" style="display:none"></div>
    <div id="lightbox" class="lightbox-overlay active">
        <img id="lightbox-img" class="lightbox-img" src="{image_url}">
    </div>
    <script>
        showLightbox("{image_url}");
    </script>
</body>
</html>"""

    webview.load_html(html, "file:///")

    is_closed = False

    def _close_dialog():
        nonlocal is_closed
        if is_closed:
            return
        is_closed = True
        dialog.destroy()

    def on_decide_policy(wv, decision, decision_type):
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            action = decision.get_navigation_action()
            uri = action.get_request().get_uri()
            if uri.startswith("opencode://close-lightbox") or uri.startswith("opencode://close-preview"):
                decision.ignore()
                GLib.idle_add(_close_dialog)
                return True
        return False

    webview.connect("decide-policy", on_decide_policy)

    def on_key_press(_widget, event):
        if event.keyval == Gdk.KEY_Escape:
            _close_dialog()
            return True
        return False

    dialog.connect("key-press-event", on_key_press)

    def on_destroy(*_):
        nonlocal is_closed
        is_closed = True
        if on_dialog_hidden:
            try:
                on_dialog_hidden()
            except Exception as e:
                print(f"Warning: error in on_dialog_hidden: {e}", flush=True)

    dialog.connect("destroy", on_destroy)
    dialog.show_all()
