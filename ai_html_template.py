"""HTML template for the AI panel WebView — extracted from clipboard_panel.py.

KaTeX CSS/JS resources are loaded at import time and cached in module-level
globals to avoid ``file://`` subresource loading issues in WebKit2GTK.

CSS and JS are loaded from ``html_templates/chat.css`` and ``chat.js`` at
import time. Missing files produce a warning but do not crash the app.
"""

import os

# ── KaTeX resource loading ────────────────────────────────────────────────────

_KATEX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "katex")

# Pre-load and cache KaTeX CSS/JS contents for inline embedding in HTML template.
# This avoids file:// subresource loading issues in WebKit2GTK.
_KATEX_INLINE_CSS: str = ""
_KATEX_INLINE_JS: str = ""
_KATEX_AUTO_RENDER_JS: str = ""
if os.path.isdir(_KATEX_DIR):
    # katex.min.css — font URLs rewritten to absolute file:// paths
    _css_path = os.path.join(_KATEX_DIR, "katex.min.css")
    if os.path.isfile(_css_path):
        try:
            with open(_css_path, "r", encoding="utf-8") as _f:
                _content = _f.read()
            _fonts_url = f"file://{_KATEX_DIR}/fonts/"
            _KATEX_INLINE_CSS = _content.replace("url(fonts/", f"url({_fonts_url}")
        except (OSError, UnicodeDecodeError) as _e:
            print(f"Warning: failed to read {_css_path}: {_e}", flush=True)

    # katex.min.js — inline, no font rewriting needed
    _js_path = os.path.join(_KATEX_DIR, "katex.min.js")
    if os.path.isfile(_js_path):
        try:
            with open(_js_path, "r", encoding="utf-8") as _f:
                _KATEX_INLINE_JS = _f.read()
        except (OSError, UnicodeDecodeError) as _e:
            print(f"Warning: failed to read {_js_path}: {_e}", flush=True)

    # auto-render.min.js — inline, no font rewriting needed
    _ar_path = os.path.join(_KATEX_DIR, "auto-render.min.js")
    if os.path.isfile(_ar_path):
        try:
            with open(_ar_path, "r", encoding="utf-8") as _f:
                _KATEX_AUTO_RENDER_JS = _f.read()
        except (OSError, UnicodeDecodeError) as _e:
            print(f"Warning: failed to read {_ar_path}: {_e}", flush=True)


# ── Pygments CSS helper ───────────────────────────────────────────────────────

def _get_pygments_css(theme: str, cache: dict) -> str:
    """Return Pygments CSS string for code highlighting, cached by theme."""
    cached = cache.get(theme)
    if cached is not None:
        return cached
    try:
        from pygments.formatters import HtmlFormatter
        style = "monokai" if theme == "dark" else "friendly"
        css = HtmlFormatter(style=style).get_style_defs(".codehilite")
    except ImportError:
        css = ""
    cache[theme] = css
    return css


# ── CSS/JS resource loading from html_templates/ ──────────────────────────────

_HTML_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "html_templates")

_CHAT_CSS: str = ""
_CHAT_JS: str = ""


def _load_resource(filename: str) -> str:
    """从 html_templates/ 目录加载资源文件。若文件缺失，返回空字符串。"""
    path = os.path.join(_HTML_TEMPLATES_DIR, filename)
    if not os.path.isfile(path):
        print(f"Warning: {path} not found, AI panel may render incorrectly", flush=True)
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"Warning: failed to read {path}: {e}", flush=True)
        return ""


# 在模块导入时加载
_CHAT_CSS = _load_resource("chat.css")
_CHAT_JS = _load_resource("chat.js")


# ── HTML template ─────────────────────────────────────────────────────────────

def get_html_template(theme_name: str, initial_html: str = "",
                      pygments_css: str = "") -> str:
    """Build the full HTML page for the AI panel WebView.

    Parameters
    ----------
    theme_name : str
        ``"dark"`` or ``"light"`` — used for colour scheme and body CSS class.
    initial_html : str
        Pre-rendered markdown HTML to place inside ``#content``.
    pygments_css : str
        Syntax-highlighting CSS from ``_get_pygments_css`` (caller-computed
        to allow caching). Pass empty string to omit highlighting.
    """
    if theme_name == "dark":
        bg_color = "#0a0b10"
        text_color = "rgba(255,255,255,0.95)"
        pre_bg = "#12131a"
        code_bg = "rgba(255,255,255,0.06)"
        code_fg = "#f43f5e"
        pre_border = "rgba(255,255,255,0.08)"
        thinking_color = "#38bdf8"
        answer_color = "#f59e0b"
        user_color = "#818cf8"
        assistant_color = "#2dd4bf"
        table_header_bg = "rgba(255,255,255,0.06)"
        table_alt_bg = "rgba(255,255,255,0.03)"
        toggle_color = "#38bdf8"
    else:
        bg_color = "#ffffff"
        text_color = "rgba(15,23,42,0.92)"
        pre_bg = "rgba(0,0,0,0.02)"
        code_bg = "rgba(0,0,0,0.04)"
        code_fg = "#e11d48"
        pre_border = "rgba(0,0,0,0.08)"
        thinking_color = "#0284c7"
        answer_color = "#d97706"
        user_color = "#6366f1"
        assistant_color = "#0d9488"
        table_header_bg = "rgba(0,0,0,0.04)"
        table_alt_bg = "rgba(0,0,0,0.02)"
        toggle_color = "#0284c7"

    # 替换 CSS 中的 {variable} 占位符
    # 注意：不能用 .format()——CSS 选择器中的 { } 会使它崩溃
    # 改用 str.replace() 逐项替换
    css_vars = {
        "bg_color": bg_color,
        "text_color": text_color,
        "pre_bg": pre_bg,
        "code_bg": code_bg,
        "code_fg": code_fg,
        "pre_border": pre_border,
        "thinking_color": thinking_color,
        "answer_color": answer_color,
        "user_color": user_color,
        "assistant_color": assistant_color,
        "table_header_bg": table_header_bg,
        "table_alt_bg": table_alt_bg,
        "toggle_color": toggle_color,
    }
    css_content = _CHAT_CSS
    for key, value in css_vars.items():
        css_content = css_content.replace("{" + key + "}", value)
    if pygments_css:
        css_content += f"\n/* Pygments syntax highlighting */\n{pygments_css}"

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>{_KATEX_INLINE_CSS}</style>
    <script>{_KATEX_INLINE_JS}</script>
    <script>{_KATEX_AUTO_RENDER_JS}</script>
    <style>{css_content}</style>
    <script>{_CHAT_JS}</script>
</head>
<body class="{theme_name}">
    <div id="show-older-bar" class="show-older-bar" style="display:none">
        <button onclick="showOlderBatch()">
            ↑ 显示更早的消息（
            <span id="hidden-count" class="hidden-count">0</span>
            轮已隐藏）
        </button>
        &nbsp;
        <button onclick="showAllMessages()" style="font-size:12px; opacity:0.7;">
            显示全部
        </button>
    </div>
    <div id="content">{initial_html}</div>
    <div id="lightbox" class="lightbox-overlay">
        <img id="lightbox-img" class="lightbox-img">
    </div>
    <div id="round-nav">
        <button id="round-top" class="nav-btn" onclick="_scrollToTopForce()" title="跳至最顶端">⤴</button>
        <button id="round-prev" class="nav-btn" onclick="_prevRound()">◀</button>
        <span id="round-indicator" class="round-indicator">1/1</span>
        <button id="round-next" class="nav-btn" onclick="_nextRound()">▶</button>
        <button id="round-bottom" class="nav-btn" onclick="_scrollToBottomForce()" title="跳至最底部">⤵</button>
    </div>
    <script>
        _scrollToBottom();
        _initRoundNav();
    </script>
</body>
</html>"""
