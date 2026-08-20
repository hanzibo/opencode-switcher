"""Theme configuration tab."""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


class ThemeTabMixin:
    """主题切换设置标签页。"""

    # ── Tab: 主题 ──────────────────────────────────────────────────────

    def _build_theme_tab(self):
        """Build the Theme configuration tab page.

        Allows switching between Dark, Light, and Dark Moon themes.
        The change is applied on Save via the ``on_theme_changed`` callback.
        """
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)

        # ── Dark / Light / Dark Moon radio buttons ──
        theme_lbl = Gtk.Label.new()
        theme_lbl.set_markup("<b>界面主题</b>")
        theme_lbl.set_xalign(0)
        vbox.pack_start(theme_lbl, False, False, 0)

        self._theme_dark_radio = Gtk.RadioButton.new_with_label(None, "经典深色 (Dark)")
        self._theme_light_radio = Gtk.RadioButton.new_with_label_from_widget(
            self._theme_dark_radio, "浅色 (Light)"
        )
        self._theme_dark_moon_radio = Gtk.RadioButton.new_with_label_from_widget(
            self._theme_dark_radio, "紫月星云 (Dark Moon)"
        )
        if self._current_theme == "light":
            self._theme_light_radio.set_active(True)
        elif self._current_theme == "dark-moon":
            self._theme_dark_moon_radio.set_active(True)
        else:
            self._theme_dark_radio.set_active(True)

        vbox.pack_start(self._theme_dark_radio, False, False, 0)
        vbox.pack_start(self._theme_dark_moon_radio, False, False, 0)
        vbox.pack_start(self._theme_light_radio, False, False, 0)

        # ── Preview hint ──
        hint = Gtk.Label.new()
        theme_names = {"light": "Light (浅色)", "dark": "Dark (经典深色)", "dark-moon": "Dark Moon (紫月星云)"}
        theme_name = theme_names.get(self._current_theme, "Dark")
        hint.set_markup(
            f"<span size='small' foreground='#888888'>"
            f"当前主题：{theme_name}。\n"
            f"更改保存后立即生效。"
            f"</span>"
        )
        hint.set_xalign(0)
        hint.set_margin_top(12)
        vbox.pack_start(hint, False, False, 0)

        # ── Spacer ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return self._make_tab_scrolled_window(vbox)
