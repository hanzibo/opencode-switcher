"""HTML template for the AI panel WebView — extracted from clipboard_panel.py.

KaTeX CSS/JS resources are loaded at import time and cached in module-level
globals to avoid ``file://`` subresource loading issues in WebKit2GTK.
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

    return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>{_KATEX_INLINE_CSS}</style>
            <script>{_KATEX_INLINE_JS}</script>
            <script>{_KATEX_AUTO_RENDER_JS}</script>
            <script>
                const KATEX_DELIMITERS = [
                    {{left: '$$', right: '$$', display: true}},
                    {{left: '$', right: '$', display: false}},
                    {{left: '\\\\(', right: '\\\\)', display: false}},
                    {{left: '\\\\[', right: '\\\\]', display: true}}
                ];
                window._isStreaming = false;

                let lightboxScale = 1.0;
                let translateX = 0;
                let translateY = 0;
                let isDragging = false;

                let startX = 0, startY = 0;
                let currentX = 0, currentY = 0;
                let dragDistance = 0;
                let rafId = null;

                document.addEventListener('DOMContentLoaded', function() {{
                    if (typeof renderMathInElement === 'function') {{
                        renderMathInElement(document.body, {{
                            delimiters: KATEX_DELIMITERS,
                            throwOnError: false,
                            errorColor: 'transparent'
                        }});
                    }}

                    const lightbox = document.getElementById('lightbox');
                    const img = document.getElementById('lightbox-img');

                    function updateTransform() {{
                        translateX = currentX;
                        translateY = currentY;
                        img.style.transform = `translate(${{translateX}}px, ${{translateY}}px) scale(${{lightboxScale}})`;
                        rafId = null;
                    }}

                    if (lightbox && img) {{
                        // Prevent system default image drag ghost image
                        img.addEventListener('dragstart', function(e) {{
                            e.preventDefault();
                        }});

                        // Double click to reset zoom & translation
                        img.addEventListener('dblclick', function(e) {{
                            e.stopPropagation();
                            if (rafId) {{
                                cancelAnimationFrame(rafId);
                                rafId = null;
                            }}
                            lightboxScale = 1.0;
                            translateX = 0;
                            translateY = 0;
                            img.style.transform = 'translate(0px, 0px) scale(1)';
                        }});

                        // Wheel Zoom
                        lightbox.addEventListener('wheel', function(e) {{
                            e.preventDefault();
                            const zoomStep = 0.08;
                            if (e.deltaY < 0) {{
                                lightboxScale = Math.min(lightboxScale + zoomStep, 5.0);
                            }} else {{
                                lightboxScale = Math.max(lightboxScale - zoomStep, 0.5);
                            }}
                            img.style.transform = `translate(${{translateX}}px, ${{translateY}}px) scale(${{lightboxScale}})`;
                        }}, {{ passive: false }});

                        // Mouse Drag
                        lightbox.addEventListener('mousedown', function(e) {{
                            if (e.button !== 0) return; // Only left button
                            isDragging = true;
                            startX = e.clientX - translateX;
                            startY = e.clientY - translateY;
                            dragDistance = 0;
                            lightbox.style.cursor = 'grabbing';
                            img.classList.add('dragging');
                        }});

                        window.addEventListener('mousemove', function(e) {{
                            if (!isDragging) return;
                            currentX = e.clientX - startX;
                            currentY = e.clientY - startY;
                            dragDistance += Math.abs(currentX - translateX) + Math.abs(currentY - translateY);
                            
                            if (!rafId) {{
                                rafId = requestAnimationFrame(updateTransform);
                            }}
                        }});

                        window.addEventListener('mouseup', function(e) {{
                            if (!isDragging) return;
                            isDragging = false;
                            lightbox.style.cursor = '';
                            img.classList.remove('dragging');
                        }});

                        // Click handler to close (only on background clicked)
                        lightbox.addEventListener('click', function(e) {{
                            if (dragDistance > 8) return;
                            if (e.target === lightbox) {{
                                closeLightbox();
                            }}
                        }});
                    }}
                }});

                function toggleToolResult(btn) {{
                    const box = btn.closest('.tool-result-box');
                    if (!box) return;
                    const content = box.querySelector('.tool-result-content');
                    if (!content) return;
                    if (content.style.display === 'none') {{
                        content.style.display = 'block';
                        btn.textContent = '收起';
                    }} else {{
                        content.style.display = 'none';
                        btn.textContent = '展开';
                    }}
                    if (typeof _scrollToBottom === 'function') {{
                        _scrollToBottom();
                    }}
                }}

                function showLightbox(src) {{
                    const lightbox = document.getElementById('lightbox');
                    const img = document.getElementById('lightbox-img');
                    if (!lightbox || !img) return;
                    img.src = src;
                    if (rafId) {{
                        cancelAnimationFrame(rafId);
                        rafId = null;
                    }}
                    img.classList.remove('dragging');
                    lightboxScale = 1.0;
                    translateX = 0;
                    translateY = 0;
                    img.style.transform = 'translate(0px, 0px) scale(1)';
                    lightbox.style.display = 'flex';
                    lightbox.offsetHeight;
                    lightbox.classList.add('active');
                }}
                function closeLightbox() {{
                    const lightbox = document.getElementById('lightbox');
                    const img = document.getElementById('lightbox-img');
                    if (img) {{
                        img.classList.remove('dragging');
                    }}
                    if (!lightbox) return;
                    lightbox.classList.remove('active');
                    setTimeout(() => {{
                        lightbox.style.display = 'none';
                    }}, 200);
                }}
                document.addEventListener('keydown', function(e) {{
                    if (e.key === 'Escape') {{
                        closeLightbox();
                    }}
                }});
            </script>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                    color: {text_color};
                    background-color: {bg_color};
                    line-height: 1.6;
                    padding: 8px;
                    margin: 0;
                    font-size: 14px;
                }}
                pre {{
                    background-color: {pre_bg};
                    padding: 32px 12px 12px 12px;
                    border-radius: 6px;
                    overflow: auto;
                    border: 1px solid {pre_border};
                    position: relative;
                }}
                code {{
                    font-family: ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, monospace;
                    font-size: 85%;
                    background-color: {code_bg};
                    padding: 2px 4px;
                    border-radius: 4px;
                    color: {code_fg};
                }}
                pre code {{
                    background-color: transparent;
                    padding: 0;
                    color: inherit;
                }}
                h1, h2, h3, h4, h5, h6 {{
                    margin-top: 16px;
                    margin-bottom: 8px;
                    font-weight: 600;
                    color: inherit;
                }}
                p {{
                    margin-top: 0;
                    margin-bottom: 8px;
                }}
                {pygments_css}
                /* Pygments classifies brackets/arrows in language-less code blocks as
                   Token.Error (.err). The monokai theme renders .err with red text+background
                   (#ED007E on #1E0010); the friendly theme uses border: 1px solid #F00.
                   Override all three properties so these characters inherit normal code text
                   color instead of appearing as distracting red boxes. */
                .codehilite .err {{ color: inherit; background-color: transparent; border: none; }}
                .tool-call-info {{
                    background: rgba(99, 102, 241, 0.08);
                    border: 1px solid rgba(99, 102, 241, 0.15);
                    border-radius: 6px;
                    padding: 6px 10px;
                    margin: 6px 0;
                    font-size: 13px;
                    font-family: ui-monospace, SFMono-Regular, monospace;
                }}
                .tool-result-box {{
                    border: 1px solid rgba(45, 212, 191, 0.15);
                    border-left: 3px solid rgba(45, 212, 191, 0.4);
                    border-radius: 6px;
                    margin: 6px 0;
                    overflow: hidden;
                    background: rgba(45, 212, 191, 0.02);
                }}
                .tool-result-header {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    background: rgba(45, 212, 191, 0.06);
                    padding: 6px 10px;
                    font-size: 13px;
                    font-weight: bold;
                    border-bottom: 1px solid rgba(45, 212, 191, 0.08);
                    user-select: none;
                }}
                .tool-result-toggle {{
                    color: {toggle_color};
                    cursor: pointer;
                    font-size: 12px;
                    user-select: none;
                    font-weight: normal;
                }}
                .tool-result-toggle:hover {{
                    text-decoration: underline;
                    opacity: 0.85;
                }}
                .tool-result-content {{
                    padding: 8px 10px;
                    margin: 0;
                    font-size: 13px;
                    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
                    white-space: pre-wrap;
                    word-break: break-all;
                    max-height: 200px;
                    overflow-y: auto;
                }}
                .copy-btn {{
                    position: absolute;
                    top: 4px;
                    right: 4px;
                    background: rgba(128,128,128,0.12);
                    border: 1px solid rgba(128,128,128,0.2);
                    border-radius: 4px;
                    color: inherit;
                    cursor: pointer;
                    font-size: 11px;
                    padding: 1px 7px;
                    opacity: 0;
                    transition: opacity 0.2s;
                    line-height: 1.6;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                }}
                pre:hover .copy-btn {{
                    opacity: 1;
                }}
                .copy-btn:hover {{
                    background: rgba(128,128,128,0.25);
                }}
                .copy-btn.copied {{
                    opacity: 1;
                }}
                .msg-copy-btn {{
                    display: inline-block;
                    background: rgba(128,128,128,0.06);
                    border: 1px solid rgba(128,128,128,0.12);
                    border-radius: 4px;
                    color: inherit;
                    cursor: pointer;
                    font-size: 11px;
                    padding: 2px 10px;
                    opacity: 0.4;
                    transition: opacity 0.2s;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                }}
                .msg-copy-btn:hover {{
                    opacity: 1;
                    background: rgba(128,128,128,0.15);
                }}
                .msg-btn-row {{
                    display: flex;
                    gap: 6px;
                    margin-top: 8px;
                }}
                .retry-btn {{
                    display: inline-block;
                    background: rgba(128,128,128,0.06);
                    border: 1px solid rgba(128,128,128,0.12);
                    border-radius: 4px;
                    color: inherit;
                    cursor: pointer;
                    font-size: 11px;
                    padding: 2px 10px;
                    opacity: 0.4;
                    transition: opacity 0.2s;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                }}
                .retry-btn:hover {{
                    opacity: 1;
                    background: rgba(128,128,128,0.15);
                }}
                .msg-copy-user-btn {{
                    opacity: 0.35;
                }}
                .msg-copy-user-btn:hover {{
                    opacity: 1;
                }}
                .thinking-header {{ color: {thinking_color}; font-weight: bold; margin-top: 12px; }}
                .answer-header {{ color: {answer_color}; font-weight: bold; margin-top: 12px; }}
                .user-header {{ color: {user_color}; font-weight: bold; margin-top: 12px; }}
                .assistant-header {{ color: {assistant_color}; font-weight: bold; margin-top: 12px; }}
                table {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
                th, td {{ border: 1px solid {pre_border}; padding: 6px 10px; text-align: left; }}
                th {{ background-color: {table_header_bg}; font-weight: 600; }}
                tr:nth-child(even) {{ background-color: {table_alt_bg}; }}
                .math-fallback {{
                    display: inline-block;
                    background: rgba(128,128,128,0.08);
                    padding: 2px 6px;
                    border-radius: 4px;
                    font-family: ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, monospace;
                    font-size: 85%;
                    white-space: pre-wrap;
                    word-break: break-all;
                }}
                .chat-image {{
                    max-width: 100%;
                    max-height: 220px;
                    border-radius: 8px;
                    border: 1px solid {pre_border};
                    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
                    cursor: zoom-in;
                    transition: all 0.2s ease;
                    margin: 6px 0;
                    display: block;
                }}
                .chat-image:hover {{
                    transform: scale(1.015);
                    border-color: {toggle_color};
                    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.22);
                }}
                .lightbox-overlay {{
                    position: fixed;
                    top: 0;
                    left: 0;
                    right: 0;
                    bottom: 0;
                    background: rgba(10, 11, 16, 0.95);
                    display: none;
                    justify-content: center;
                    align-items: center;
                    z-index: 9999;
                    cursor: zoom-out;
                    opacity: 0;
                    transition: opacity 0.2s ease;
                }}
                .lightbox-overlay.active {{
                    opacity: 1;
                }}
                .lightbox-img {{
                    max-width: 90%;
                    max-height: 90%;
                    object-fit: contain;
                    border-radius: 6px;
                    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
                    transform: scale(0.95);
                    transition: transform 0.2s ease;
                }}
                .lightbox-overlay.active .lightbox-img {{
                    transform: scale(1);
                }}
                .lightbox-img.dragging {{
                    transition: none !important;
                }}
                
                /* --- Card-Bubble UI Styles --- */
                hr {{
                    display: none !important;
                }}
                
                /* Hide redundant headers inside bubbles */
                .user-header, .assistant-header, .answer-header {{
                    display: none !important;
                }}
                
                /* Message Row Layout */
                .msg-row {{
                    display: flex;
                    align-items: flex-start;
                    margin-bottom: 20px;
                    gap: 12px;
                    position: relative;
                    box-sizing: border-box;
                    width: 100%;
                }}
                .msg-row.user {{
                    flex-direction: row-reverse;
                }}
                .msg-avatar {{
                    width: 32px;
                    height: 32px;
                    border-radius: 50%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    flex-shrink: 0;
                    user-select: none;
                    box-sizing: border-box;
                }}
                .msg-avatar.user {{
                    background: linear-gradient(135deg, #6366f1, #4f46e5);
                    color: #ffffff;
                    border: 1px solid rgba(99, 102, 241, 0.35);
                    box-shadow: 0 4px 10px rgba(99, 102, 241, 0.22);
                }}
                .msg-avatar.assistant {{
                    background: linear-gradient(135deg, #06b6d4, #0891b2);
                    color: #ffffff;
                    border: 1px solid rgba(6, 182, 212, 0.35);
                    box-shadow: 0 4px 10px rgba(6, 182, 212, 0.22);
                }}
                .msg-bubble {{
                    position: relative;
                    max-width: 82%;
                    padding: 12px 16px;
                    border-radius: 16px;
                    font-size: 14px;
                    line-height: 1.6;
                    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.06);
                    box-sizing: border-box;
                }}
                .msg-bubble.user {{
                    background: rgba(99, 102, 241, 0.1);
                    border: 1px solid rgba(99, 102, 241, 0.18);
                    border-bottom-right-radius: 4px;
                    color: inherit;
                    width: fit-content;
                }}
                .msg-bubble.assistant {{
                    background: rgba(255, 255, 255, 0.02);
                    border: 1px solid rgba(255, 255, 255, 0.04);
                    border-bottom-left-radius: 4px;
                    width: calc(100% - 44px);
                    padding-bottom: 32px; /* room for absolute copy/retry row */
                }}
                
                .light .msg-bubble.user {{
                    background: rgba(99, 102, 241, 0.07);
                    border-color: rgba(99, 102, 241, 0.15);
                    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.03);
                }}
                .light .msg-bubble.assistant {{
                    background: rgba(0, 0, 0, 0.015);
                    border-color: rgba(0, 0, 0, 0.04);
                }}
                
                .user-content {{
                    word-break: break-word;
                }}
                
                /* Muted and collapsible thinking styles */
                details.thinking-details {{
                    margin: 8px 0 14px 0;
                    padding: 8px 12px;
                    background: rgba(128, 128, 128, 0.05);
                    border: 1px solid rgba(128, 128, 128, 0.12);
                    border-radius: 8px;
                    font-size: 13px;
                    transition: all 0.2s ease;
                }}
                
                details.thinking-details[open] {{
                    background: rgba(128, 128, 128, 0.07);
                }}
                
                summary.thinking-summary {{
                    font-weight: 500;
                    color: {thinking_color};
                    cursor: pointer;
                    outline: none;
                    user-select: none;
                }}
                
                summary.thinking-summary::-webkit-details-marker {{
                    color: {thinking_color};
                    margin-right: 6px;
                }}
                
                .thinking-content {{
                    margin-top: 8px;
                    padding-top: 8px;
                    border-top: 1px dashed rgba(128, 128, 128, 0.15);
                    color: rgba(255, 255, 255, 0.7);
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    white-space: pre-wrap;
                    line-height: 1.5;
                }}
                
                .light details.thinking-details {{
                    background: rgba(0, 0, 0, 0.02);
                    border: 1px solid rgba(0, 0, 0, 0.06);
                }}
                
                .light .thinking-content {{
                    color: rgba(15, 23, 42, 0.75);
                }}
                
                /* Tool Steps Timeline Styles */
                .tool-steps-container {{
                    margin: 10px 0;
                    display: flex;
                    flex-direction: column;
                    gap: 8px;
                    border-left: 2px solid rgba(128, 128, 128, 0.15);
                    padding-left: 12px;
                    margin-left: 6px;
                }}
                details.tool-step-details {{
                    background: rgba(128, 128, 128, 0.04);
                    border: 1px solid rgba(128, 128, 128, 0.1);
                    border-radius: 6px;
                    font-size: 13px;
                    overflow: hidden;
                }}
                details.tool-step-details[open] {{
                    background: rgba(128, 128, 128, 0.06);
                }}
                summary.tool-step-summary {{
                    padding: 6px 10px;
                    cursor: pointer;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    user-select: none;
                    outline: none;
                    font-weight: 500;
                }}
                .tool-step-status {{
                    font-size: 14px;
                    display: inline-block;
                }}
                .tool-step-status.running {{
                    animation: spin 1s linear infinite;
                }}
                @keyframes spin {{
                    100% {{ transform: rotate(360deg); }}
                }}
                .tool-step-time {{
                    margin-left: auto;
                    color: rgba(128, 128, 128, 0.6);
                    font-size: 11px;
                }}
                .tool-step-content {{
                    padding: 8px 10px;
                    border-top: 1px solid rgba(128, 128, 128, 0.08);
                    background: rgba(0, 0, 0, 0.15);
                }}
                .tool-step-args {{
                    font-family: monospace;
                    font-size: 12px;
                    margin-bottom: 6px;
                    color: rgba(255, 255, 255, 0.7);
                    word-break: break-all;
                }}
                .tool-step-result {{
                    margin: 0;
                }}
                .tool-step-result pre {{
                    margin: 0 !important;
                    padding: 8px !important;
                    background: rgba(0, 0, 0, 0.3) !important;
                    border: 1px solid rgba(128, 128, 128, 0.1) !important;
                    border-radius: 4px !important;
                    font-size: 12px !important;
                    max-height: 200px;
                    overflow: auto;
                }}
                .light details.tool-step-details {{
                    background: rgba(0, 0, 0, 0.02);
                    border-color: rgba(0, 0, 0, 0.06);
                }}
                .light .tool-step-content {{
                    background: rgba(0, 0, 0, 0.03);
                }}
                .light .tool-step-result pre {{
                    background: rgba(255, 255, 255, 0.8) !important;
                    border-color: rgba(0, 0, 0, 0.08) !important;
                    color: inherit !important;
                }}
                .light .tool-step-args {{
                    color: rgba(15, 23, 42, 0.6);
                }}

                /* Action Button Row Hover-Reveal & Pill Styling */
                .msg-btn-row {{
                    position: absolute;
                    bottom: 6px;
                    right: 12px;
                    display: flex;
                    gap: 6px;
                    opacity: 0;
                    transition: opacity 0.2s ease;
                    z-index: 10;
                }}
                
                .msg-bubble:hover .msg-btn-row {{
                    opacity: 1;
                }}
                
                .msg-copy-btn, .retry-btn {{
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    background: rgba(128, 128, 128, 0.12);
                    border: 1px solid rgba(128, 128, 128, 0.2);
                    border-radius: 6px;
                    color: inherit;
                    cursor: pointer;
                    font-size: 11px;
                    padding: 3px 8px;
                    transition: all 0.2s ease;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    outline: none;
                    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
                    opacity: 1 !important; /* override old opacity rules */
                }}
                
                .msg-copy-btn:hover, .retry-btn:hover {{
                    background: rgba(128, 128, 128, 0.22);
                    border-color: rgba(128, 128, 128, 0.3);
                    transform: translateY(-1px);
                }}
                
                .msg-copy-btn:active, .retry-btn:active {{
                    transform: translateY(0);
                }}
                
                .light .msg-copy-btn, .light .retry-btn {{
                    background: rgba(0, 0, 0, 0.05);
                    border-color: rgba(0, 0, 0, 0.12);
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
                }}
                
                .light .msg-copy-btn:hover, .light .retry-btn:hover {{
                    background: rgba(0, 0, 0, 0.08);
                }}

                /* Typing Indicator Styling */
                .typing-indicator {{
                    display: flex;
                    align-items: center;
                    gap: 4px;
                    padding: 6px 0;
                    width: fit-content;
                }}
                .typing-dot {{
                    width: 6px;
                    height: 6px;
                    border-radius: 50%;
                    background: rgba(255, 255, 255, 0.45);
                    animation: typing-bounce 1.2s infinite ease-in-out;
                }}
                .light .typing-dot {{
                    background: rgba(15, 23, 42, 0.4);
                }}
                .typing-dot:nth-child(1) {{ animation-delay: 0s; }}
                .typing-dot:nth-child(2) {{ animation-delay: 0.15s; }}
                .typing-dot:nth-child(3) {{ animation-delay: 0.3s; }}
                @keyframes typing-bounce {{
                    0%, 80%, 100% {{ transform: scale(0.6); opacity: 0.4; }}
                    40% {{ transform: scale(1.0); opacity: 1; }}
                }}

                /* Code Language Pill Styling */
                pre {{
                    position: relative;
                    padding-top: 32px !important;
                }}
                pre::before {{
                    content: attr(data-lang);
                    position: absolute;
                    top: 0;
                    left: 0;
                    font-size: 10px;
                    font-weight: bold;
                    letter-spacing: 0.8px;
                    padding: 4px 10px;
                    border-radius: 6px 0 6px 0;
                    background: rgba(128, 128, 128, 0.15);
                    color: rgba(255, 255, 255, 0.6);
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                }}
                .light pre::before {{
                    color: rgba(15, 23, 42, 0.5);
                    background: rgba(0, 0, 0, 0.05);
                }}
            </style>
            <script>
                function _renderMath(element) {{
                    if (window._isStreaming) return;
                    if (typeof renderMathInElement === 'function') {{
                        renderMathInElement(element || document.body, {{
                            delimiters: KATEX_DELIMITERS,
                            throwOnError: false,
                            errorColor: 'transparent'
                        }});
                    }}
                    (element || document.body).querySelectorAll('.katex-error').forEach(function(el) {{
                        if (el.closest('.math-fallback')) return;
                        var wrapper = document.createElement('code');
                        wrapper.className = 'math-fallback';
                        wrapper.textContent = el.textContent;
                        el.replaceWith(wrapper);
                    }});
                }}
                const SCROLL_THRESHOLD = 20;
                let _autoScroll = true;
                window.addEventListener('scroll', function() {{
                    _autoScroll = (window.innerHeight + window.scrollY >= document.body.scrollHeight - SCROLL_THRESHOLD);
                }});
                function _scrollToBottom() {{
                    if (_autoScroll) {{
                        window.scrollTo(0, document.body.scrollHeight);
                    }}
                }}
                function updateContent(html) {{
                    window._isStreaming = false;
                    const content = document.getElementById('content');
                    content.innerHTML = html;
                    addCopyButtons();
                    _renderMath(content);
                    _scrollToBottom();
                }}
                function appendMessageContainer(msgId) {{
                    window._isStreaming = true;
                    const content = document.getElementById('content');
                    if (!document.getElementById(msgId)) {{
                        const row = document.createElement('div');
                        row.id = msgId;
                        row.className = 'msg-row assistant';
                        
                        const avatar = document.createElement('div');
                        avatar.className = 'msg-avatar assistant';
                        avatar.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 2L14.8 9.2L22 12L14.8 14.8L12 22L9.2 14.8L2 12L9.2 9.2L12 2Z"/></svg>';
                        row.appendChild(avatar);
                        
                        const bubble = document.createElement('div');
                        bubble.className = 'msg-bubble assistant';
                        bubble.id = msgId + '-bubble';
                        bubble.innerHTML = '<div class="typing-indicator"><div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div></div>';
                        row.appendChild(bubble);
                        
                        content.appendChild(row);
                    }}
                    _scrollToBottom();
                }}
                function updateMessageContainer(msgId, html) {{
                    const div = document.getElementById(msgId + '-bubble') || document.getElementById(msgId);
                    if (div) {{
                        div.innerHTML = html;
                        addCopyButtons();
                        _renderMath(div);
                    }}
                    _scrollToBottom();
                }}
                function appendHtml(msgId, html) {{
                    const div = document.getElementById(msgId + '-bubble') || document.getElementById(msgId);
                    if (div && html) {{
                        div.insertAdjacentHTML('beforeend', html);
                        _renderMath(div);
                        addCopyButtons();
                    }}
                    _scrollToBottom();
                }}
                function addCopyButtons() {{
                    document.querySelectorAll('pre').forEach(function(pre) {{
                        if (pre.classList.contains('tool-result-content')) return;
                        
                        const code = pre.querySelector('code');
                        if (code) {{
                            let lang = 'CODE';
                            code.classList.forEach(function(cls) {{
                                if (cls.startsWith('language-')) {{
                                    lang = cls.replace('language-', '').toUpperCase();
                                }}
                            }});
                            pre.setAttribute('data-lang', lang);
                        }}

                        if (pre.querySelector('.copy-btn')) return;
                        const btn = document.createElement('button');
                        btn.className = 'copy-btn';
                        btn.textContent = '复制';
                        btn.addEventListener('click', function() {{
                            const code = pre.querySelector('code');
                            const text = code ? code.textContent : pre.textContent;
                            if (navigator.clipboard && navigator.clipboard.writeText) {{
                                navigator.clipboard.writeText(text).then(function() {{
                                    btn.textContent = '✓';
                                    btn.classList.add('copied');
                                    setTimeout(function() {{ btn.textContent = '复制'; btn.classList.remove('copied'); }}, 2000);
                                }}).catch(function(e) {{
                                    console.warn('Copy failed, trying fallback:', e);
                                    fallbackCopy(text, function() {{
                                        btn.textContent = '✓';
                                        btn.classList.add('copied');
                                        setTimeout(function() {{ btn.textContent = '复制'; btn.classList.remove('copied'); }}, 2000);
                                    }});
                                }});
                            }} else {{
                                fallbackCopy(text, function() {{
                                    btn.textContent = '✓';
                                    btn.classList.add('copied');
                                    setTimeout(function() {{ btn.textContent = '复制'; btn.classList.remove('copied'); }}, 2000);
                                }});
                            }}
                        }});
                        pre.appendChild(btn);
                    }});
                    function fallbackCopy(text, done) {{
                        const ta = document.createElement('textarea');
                        ta.value = text;
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand('copy');
                        document.body.removeChild(ta);
                        done();
                    }}
                    addMessageCopyButtons();
                    addRetryButtons();
                    addUserMessageCopyButtons();
                }}
                function _addCopyButtonsForMarkers(selector, btnText, uriPrefix, idxPrefix) {{
                    document.querySelectorAll(selector).forEach(function(marker) {{
                        var idx = marker.dataset.msgIndex;
                        var dataIdx = idxPrefix + idx;
                        if (marker.parentNode?.querySelector('.msg-btn-row[data-idx="' + dataIdx + '"]')) return;
                        var row = document.createElement('div');
                        row.className = 'msg-btn-row';
                        row.setAttribute('data-idx', dataIdx);
                        const btn = document.createElement('button');
                        btn.className = 'msg-copy-btn' + (idxPrefix ? ' msg-copy-user-btn' : '');
                        btn.textContent = btnText;
                        btn.addEventListener('click', function(e) {{
                            e.stopPropagation();
                            window.location = uriPrefix + '?index=' + idx;
                        }});
                        row.appendChild(btn);
                        marker.parentNode.insertBefore(row, marker);
                    }});
                }}
                function addMessageCopyButtons() {{
                    _addCopyButtonsForMarkers('copy-marker:not(.user-copy-marker)', '📋 复制回答', 'opencode://copy-response', '');
                }}
                function addRetryButtons() {{
                    var markers = document.querySelectorAll('copy-marker:not(.user-copy-marker)');
                    var lastIdx = -1;
                    markers.forEach(function(m) {{
                        var idx = parseInt(m.dataset.msgIndex);
                        if (!isNaN(idx) && idx > lastIdx) lastIdx = idx;
                    }});
                    if (lastIdx < 0) return;
                    var row = document.querySelector('.msg-btn-row[data-idx="' + lastIdx + '"]');
                    if (!row || row.querySelector('.retry-btn')) return;
                    var btn = document.createElement('button');
                    btn.className = 'retry-btn';
                    btn.textContent = '🔄 重新生成';
                    btn.addEventListener('click', function(e) {{
                        e.stopPropagation();
                        window.location = 'opencode://retry?index=' + lastIdx;
                    }});
                    row.appendChild(btn);
                }}
                function addUserMessageCopyButtons() {{
                    _addCopyButtonsForMarkers('copy-marker.user-copy-marker', '📋 复制输入', 'opencode://copy-input', 'u-');
                }}
            </script>
        </head>
        <body class="{theme_name}">
            <div id="content">{initial_html}</div>
            <div id="lightbox" class="lightbox-overlay">
                <img id="lightbox-img" class="lightbox-img">
            </div>
            <script>
                _scrollToBottom();
            </script>
        </body>
        </html>
        """
