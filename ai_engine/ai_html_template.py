"""HTML template for the AI panel WebView — extracted from clipboard_panel.py.

KaTeX CSS/JS resources are loaded at import time and cached in module-level
globals to avoid ``file://`` subresource loading issues in WebKit2GTK.

CSS and JS are loaded from ``html_templates/chat.css`` and ``chat.js`` at
import time. Missing files produce a warning but do not crash the app.
"""

import functools
import os

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_PKG_DIR)

_KATEX_DIR = os.path.join(_PROJECT_ROOT, "katex")
if not os.path.isdir(_KATEX_DIR):
    _KATEX_DIR = os.path.join(_PKG_DIR, "katex")

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
        style = "friendly" if theme == "light" else "monokai"
        css = HtmlFormatter(style=style).get_style_defs(".codehilite")
    except ImportError:
        css = ""
    cache[theme] = css
    return css


# ── CSS/JS resource loading from html_templates/ ──────────────────────────────

_HTML_TEMPLATES_DIR = os.path.join(_PROJECT_ROOT, "html_templates")
if not os.path.isdir(_HTML_TEMPLATES_DIR):
    _HTML_TEMPLATES_DIR = os.path.join(_PKG_DIR, "html_templates")

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


_CHAT_CSS = _load_resource("chat.css")
_CHAT_JS = _load_resource("chat.js")

_SHARED_WEB_CONTEXT = None
_MPS = None


# ── HTML shell cache ──────────────────────────────────────────────────────────

# Marker substituted with ``initial_html`` after shell retrieval. Must never
# appear inside any embedded asset; checked once at import time below.
_INITIAL_HTML_MARKER = "__INITIAL_HTML_SLOT__"

# Bound for the shell LRU. Real keys are (theme_name, pygments_css) with a
# handful of combinations (dark/light × monokai/friendly/empty), so 16 entries
# is far beyond the working set while still bounding memory.
_HTML_SHELL_CACHE_MAX = 16

for _asset in (_KATEX_INLINE_CSS, _KATEX_INLINE_JS, _KATEX_AUTO_RENDER_JS,
               _CHAT_CSS, _CHAT_JS):
    if _INITIAL_HTML_MARKER in _asset:
        print(
            f"Warning: {_INITIAL_HTML_MARKER} found in an embedded asset; "
            "content substitution would corrupt the shell",
            flush=True,
        )


# ── Nebula spinner (紫月星云) header assets ───────────────────────────────────
# Inlined into the WebView shell so no second WebKit process is spawned.
# {crescent_a} / {crescent_b} / {orbit} / {dust} / {glow} are substituted from
# stores.theme_config.get_ai_spinner_vars().

_NEBULA_CSS = """
  /* 紫月星云 spinner（#ai-header 内联） */
  #ai-header-spinner { width: 36px; height: 36px; flex: none; }
  #ai-header-spinner svg { width: 100%; height: 100%; display: block; }
  .crescent {
    animation: nebula-spin 1.4s cubic-bezier(0.4, 0, 0.2, 1) infinite;
    transform-box: fill-box; transform-origin: center;
    filter: drop-shadow(0 0 3px {glow});
  }
  .orbit {
    animation: nebula-spin-rev 4.2s linear infinite;
    transform-box: fill-box; transform-origin: center;
  }
  .orbit-ring {
    stroke: {orbit}; fill: none; stroke-width: 1.2;
    stroke-dasharray: 2.6 3.4; stroke-linecap: round;
    animation: nebula-pulse 2.4s ease-in-out infinite;
    transform-box: fill-box; transform-origin: center;
  }
  .dust { fill: {dust}; animation: nebula-flicker 2.4s ease-in-out infinite; }
  .d2 { animation-delay: 0.6s; }
  .d3 { animation-delay: 1.2s; }
  .d4 { animation-delay: 1.8s; }
  @keyframes nebula-spin     { from { transform: rotate(0deg); }   to { transform: rotate(360deg); } }
  @keyframes nebula-spin-rev { from { transform: rotate(0deg); }   to { transform: rotate(-360deg); } }
  @keyframes nebula-pulse    { 0%, 100% { opacity: 0.4; transform: scale(0.95); }
                               50%      { opacity: 0.8; transform: scale(1.05); } }
  @keyframes nebula-flicker  { 0%, 100% { opacity: 0.2; } 50% { opacity: 1; } }
"""

_NEBULA_SVG = """
  <svg viewBox="0 0 48 48">
    <defs>
      <linearGradient id="crescentGrad" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="{crescent_a}"/>
        <stop offset="100%" stop-color="{crescent_b}"/>
      </linearGradient>
    </defs>
    <g class="orbit">
      <circle class="orbit-ring" cx="24" cy="24" r="21"/>
    </g>
    <circle class="crescent" cx="24" cy="24" r="14"
            stroke="url(#crescentGrad)" stroke-width="8.5" fill="none"
            stroke-linecap="round" stroke-dasharray="56 32"/>
    <circle class="dust d1" cx="9"  cy="13" r="1.4"/>
    <circle class="dust d2" cx="39" cy="17" r="1.1"/>
    <circle class="dust d3" cx="13" cy="38" r="1.6"/>
    <circle class="dust d4" cx="36" cy="33" r="1.0"/>
  </svg>
"""


@functools.lru_cache(maxsize=_HTML_SHELL_CACHE_MAX)
def _get_html_shell(theme_name: str, pygments_css: str) -> str:
    """Build the static WebView shell (head assets, body frame) for a
    ``(theme_name, pygments_css)`` variant.

    The shell is expensive to assemble (large KaTeX/CSS/JS embedding plus 13
    theme-variable CSS replacements) but does not depend on the conversation
    content, so it is cached keyed by theme and pygments CSS. The returned
    string contains exactly one ``_INITIAL_HTML_MARKER`` occurrence inside
    ``#content``, substituted by ``get_html_template()`` on retrieval.
    """
    from stores.theme_config import get_web_css_vars, get_ai_spinner_vars
    css_vars = dict(get_web_css_vars(theme_name))
    css_vars.update(get_ai_spinner_vars(theme_name))
    css_content = _CHAT_CSS
    nebula_css = _NEBULA_CSS
    nebula_svg = _NEBULA_SVG
    for key, value in css_vars.items():
        css_content = css_content.replace("{" + key + "}", value)
        nebula_css = nebula_css.replace("{" + key + "}", value)
        nebula_svg = nebula_svg.replace("{" + key + "}", value)
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
    <style>{nebula_css}</style>
    <script>{_CHAT_JS}</script>
</head>
<body class="{theme_name}">
    <!-- 固定装饰区（原 GTK header 迁移至 WebView 内，不随消息滚动） -->
    <div id="ai-header" class="ai-header">
        <div id="ai-header-left" class="ai-header-left">
            <div id="ai-header-title" class="ai-header-title"><b>AI 助手看盘</b></div>
            <div id="ai-header-model" class="ai-header-model"></div>
        </div>
        <div id="ai-header-spinner" class="ai-header-spinner" style="display:none">{nebula_svg}</div>
        <button id="ai-history-btn" class="ai-header-btn" onclick="toggleHistoryDropdown()"><span class="ai-hdr-lbl">历史对话</span><svg class="ai-hdr-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg></button>
        <button id="ai-close-btn" class="ai-header-btn" onclick="closeAIPanel()" title="关闭AI面板"><svg class="ai-hdr-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <!-- 历史对话下拉面板 -->
    <div id="ai-history-dropdown" class="ai-history-dropdown" style="display:none">
        <div id="history-confirm-bar" class="history-confirm-bar" style="display:none">
            <span id="history-confirm-msg"></span>
            <button class="ai-history-action" id="history-confirm-ok" onclick="confirmOk()">确认</button>
            <button class="ai-history-action" id="history-confirm-cancel" onclick="confirmCancel()">取消</button>
        </div>
        <input id="ai-history-search" class="ai-history-search" type="text" placeholder="搜索对话..." autocomplete="off" spellcheck="false">
        <div id="ai-history-list"></div>
        <div class="ai-history-actions">
            <button class="ai-history-action" id="history-select-all-btn" onclick="historySelectAll()" style="display:none">全选</button>
            <button class="ai-history-action" id="history-delete-sel-btn" onclick="historyDeleteSelected()" style="display:none">删除选中</button>
            <button class="ai-history-action" onclick="historyAction('clear')">清空已删除</button>
            <button class="ai-history-action" id="history-edit-btn" onclick="historyAction('edit')">编辑</button>
        </div>
    </div>
    <div id="content">{_INITIAL_HTML_MARKER}
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
    </div>
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
        addCopyButtons();
        _renderMath(document.getElementById('content'));
        _throttledWindowing();
        _scrollToBottom();
        _initRoundNav();
    </script>
</body>
</html>"""


def get_shared_web_context():
    """Return singleton WebKit2.WebContext shared across WebViews to avoid duplicate WebKitNet processes."""
    global _SHARED_WEB_CONTEXT, _MPS
    if _SHARED_WEB_CONTEXT is not None:
        return _SHARED_WEB_CONTEXT
    try:
        import gi
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            try:
                gi.require_version("WebKit2", "4.0")
            except ValueError:
                pass
        from gi.repository import WebKit2
        _mps = WebKit2.MemoryPressureSettings.new()
        _mps.set_memory_limit(300)
        _mps.set_poll_interval(5)
        _mps.set_conservative_threshold(0.2)
        _mps.set_strict_threshold(0.4)
        _MPS = _mps
        ctx = WebKit2.WebContext.new_with_context(_MPS) if hasattr(WebKit2.WebContext, "new_with_context") else WebKit2.WebContext.new()
        ctx.set_cache_model(WebKit2.CacheModel.DOCUMENT_VIEWER)
        _SHARED_WEB_CONTEXT = ctx
        return _SHARED_WEB_CONTEXT
    except Exception as e:
        print(f"Warning: failed to initialize shared WebContext: {e}", flush=True)
        return None

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

    The static shell (KaTeX CSS/JS, themed CSS, chat JS, body frame) is
    assembled once per ``(theme_name, pygments_css)`` variant and cached in
    ``_get_html_shell``; only the variable ``initial_html`` is substituted
    here, so repeated loads skip the expensive rebuild.
    """
    shell = _get_html_shell(theme_name, pygments_css)
    return shell.replace(_INITIAL_HTML_MARKER, initial_html)
