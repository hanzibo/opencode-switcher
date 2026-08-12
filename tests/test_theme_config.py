import unittest
import os
import json
from unittest.mock import patch, mock_open
from stores.theme_config import (
    get_theme,
    get_panel_css_vals,
    get_web_css_vars,
    get_ai_gtk_colors,
    load_theme_config,
    save_theme_config,
    DARK_MOON,
    _THEMES,
)


class TestThemeConfig(unittest.TestCase):

    def test_dark_moon_theme_registered(self):
        """Test that dark-moon theme is present in _THEMES registry."""
        self.assertIn("dark-moon", _THEMES)
        theme = get_theme("dark-moon")
        self.assertEqual(theme["web_bg"], "#0f0914")
        self.assertEqual(theme["sel_border"], "#c084fc")

    def test_get_panel_css_vals_dark_moon(self):
        """Test GTK CSS interpolation dictionary for dark-moon theme."""
        vals = get_panel_css_vals("dark-moon")
        self.assertEqual(vals["search_bg"], "#181124")
        self.assertEqual(vals["sel_border"], "#c084fc")
        self.assertEqual(vals["window_border"], "rgba(168,85,247,0.18)")

    def test_get_web_css_vars_dark_moon(self):
        """Test WebKit CSS variables for dark-moon theme."""
        web_vars = get_web_css_vars("dark-moon")
        self.assertEqual(web_vars["bg_color"], "#0f0914")
        self.assertEqual(web_vars["thinking_color"], "#c084fc")
        self.assertEqual(web_vars["answer_color"], "#f472b6")
        self.assertEqual(web_vars["user_color"], "#a855f7")

    def test_get_ai_gtk_colors_dark_moon(self):
        """Test GTK RGBA colors for AI panel in dark-moon theme."""
        gtk_colors = get_ai_gtk_colors("dark-moon")
        self.assertEqual(gtk_colors["bg"], (0.059, 0.035, 0.078, 1.0))

    def test_load_and_save_theme_config(self):
        """Test persisting dark-moon theme choice."""
        mock_data = json.dumps({"theme": "dark-moon"})
        with patch("builtins.open", mock_open(read_data=mock_data)):
            theme_name = load_theme_config()
            self.assertEqual(theme_name, "dark-moon")


if __name__ == "__main__":
    unittest.main()
