"""Constants, regular expressions, and WebView bridge objects for AI Chat."""

import html
import json
import re
from typing import List, Dict

from stores.clipboard_store import ChatMessage

# Regex to match placeholders: ${index[:prompt][=default]}
# - Group 1: index (\d+)
# - Group 2: optional prompt, allowing escaped colons (\:) and equals (\=)
# - Group 3: optional default value, matched if the leading '=' is not escaped (?<\\!)
TEMPLATE_REGEX = re.compile(r"\$\{(\d+)(?::((?:[^}=]|\\:|\\=)+))?(?<!\\)(?:=([^}]*))?\}")
PROMPT_PLACEHOLDER_RE = re.compile(r'\\\\|\\(\${&})|(\${&})')

# WebView memory pressure settings — applied at WebContext construction time
_MPS_MEMORY_LIMIT = 300
_MPS_POLL_INTERVAL = 5
_MPS_CONSERVATIVE = 0.2
_MPS_STRICT = 0.4

AI_BTN_LABEL_SEND = "发送"
AI_BTN_LABEL_STOP = "暂停"
# 面板标题常量：ai_html_template.py 的 #ai-header-title 内联同名文本
_AI_HEADER_TITLE = "AI 助手看盘"

# Slash commands available in the AI chat input box (command, description)
_AI_COMMANDS = [
    ("/new", "新对话"),
    ("/delete", "删除并新建"),
    ("/fork", "建立当前对话的分支 (Fork)"),
    ("/retry", "回滚到上一轮"),
    ("/rollback", "回滚到任意轮"),
    ("/title", "设置/生成标题"),
    ("/model", "切换模型"),
    ("/cd", "切换 bash 工作路径"),
    ("/summary", "压缩上下文（/summary keep=N，保留最近N条，默认50）"),
    ("/skill", "查看与手动触发 AI Skill"),
    ("/ai-polish", "扩展润色提问，去除歧义与不严谨"),
]


def _to_chat_messages(msgs: List[Dict]) -> List[ChatMessage]:
    return [ChatMessage(role=m["role"], content=m["content"], 
                        tool_call_id=m.get("tool_call_id"),
                        name=m.get("name"),
                        tool_calls=m.get("tool_calls"),
                        reasoning_content=m.get("reasoning_content")) for m in msgs]


def _ai_stream_request_key(conv_id: str, req_id: int) -> tuple:
    """为一次主 ReAct 流生成稳定请求键。

    键 = (conv_id, req_id)，跨 ReAct 多轮迭代复用同一个键；重试/新请求会
    递增 req_id 得到新键，不与被取消的旧流冲突。并行会话键互不相同，
    取消一个不会误伤另一个。
    """
    return ("ai", conv_id, req_id)


def _ai_summary_request_key(conv_id: str) -> tuple:
    """为摘要流生成稳定请求键（同一会话的摘要流全局唯一，可被定向取消）。"""
    return ("summary", conv_id)


def _webview_shell_fingerprint(theme_name: str, pygments_css: str) -> tuple:
    """WebView 外壳指纹：(theme, pygments_css)。

    与 ``ai_engine.ai_html_template._get_html_shell`` 的 LRU 缓存键完全一致——
    主题或代码高亮样式任一变化，指纹即变化，此时必须完整重载外壳。
    """
    return (theme_name, pygments_css)


def _should_full_reload_webview(loaded_fingerprint, requested_fingerprint,
                                webview_live: bool, webview_suspended: bool,
                                webview_ready: bool) -> bool:
    """决定是否需要完整 ``load_html`` 而非 in-place ``updateContent`` 换内容。

    任一条件命中都必须完整重载（指纹守卫绝不能吞掉这些必需的重载）：
    - WebView 未构建或 DOM 不可用（webview_live=False）
    - Web 进程已被 suspend 主动终止（webview_suspended=True，恢复必须重建 DOM）
    - 文档尚未就绪（webview_ready=False）——上一轮 load_html 还在装载中，
      in-place JS 可能打到未加载完的文档而静默失败
    - 请求的外壳（主题/pygments CSS）与当前已加载的不一致

    仅当 DOM 存活、文档就绪且外壳指纹一致时返回 False —— 此时内容可原地替换。
    """
    if not webview_live:
        return True
    if webview_suspended:
        return True
    if not webview_ready:
        return True
    return loaded_fingerprint != requested_fingerprint


# ── JS 桥对象：WebView 化 header 的接口兼容层 ────────────────────────────────
# 原 GTK header 组件（Gtk.Spinner / Gtk.Label / HistoryPopover）已迁移到
# WebView 内 HTML（#ai-header）。以下桥对象保持 _ai_spinner / _ai_lbl /
# _ai_history_popover 的既有接口（show/start/stop/hide、set_markup、
# refresh_dropdown/popdown），内部转发到 WebView JS，
# 使既有调用点与测试 mock 零改动。

# set_markup 的标题/模型名解析正则
_MARKUP_TITLE_RE = re.compile(r"(<b>.*?</b>)", re.S)
_MARKUP_MODEL_RE = re.compile(r"<span[^>]*>(.*?)</span>", re.S)


class _WebViewBridgeBase:
    """桥共享基类：统一 webview 访问（None 守卫 + 吞异常）。"""

    def _run_webview_js(self, js):
        """统一 webview 访问：None 守卫 + 吞异常（桥模式惯用法）。"""
        try:
            wv = getattr(self._panel, "_ai_webview", None)
            if wv is None:
                return
            wv.run_javascript(js, None, None)
        except Exception:
            pass


class _HeaderSpinnerBridge(_WebViewBridgeBase):
    """spinner 控制 → WebView JS（兼容 show/start/stop/hide）。"""

    def __init__(self, panel):
        self._panel = panel

    def show(self):
        self._run_webview_js("showHeaderSpinner();")

    def hide(self):
        self._run_webview_js("hideHeaderSpinner();")

    def start(self):
        pass  # CSS 动画自动循环，无需手动启动

    def stop(self):
        pass  # 可见性由 GTK show/hide 控制


class _HeaderTitleBridge(_WebViewBridgeBase):
    """标题/模型名更新 → WebView JS（兼容 set_markup）。"""

    def __init__(self, panel):
        self._panel = panel
        # 显式初始化：resume 重新应用前从未 set_markup 时，getattr 能取到 None
        self._ai_last_header_markup = None
        # resume 整页重建登记标记：FINISHED 后由 _on_webview_load_changed 消费
        self._pending_header_reapply = False

    def set_markup(self, markup):
        try:
            # 先记录最近一次 markup（webview 暂缺窗口内也保留意图，resume 时重放）
            self._ai_last_header_markup = markup
            m = _MARKUP_TITLE_RE.search(markup)
            title_html = m.group(1).strip() if m else html.escape(markup)
            m = _MARKUP_MODEL_RE.search(markup)
            model_text = m.group(1).strip() if m else ""
            js = "updateHeaderTitle(%s, %s);" % (
                json.dumps(title_html, ensure_ascii=False),
                json.dumps(model_text, ensure_ascii=False),
            )
            self._run_webview_js(js)
        except Exception:
            pass


class _HistoryPopoverBridge(_WebViewBridgeBase):
    """历史下拉 → WebView JS（兼容 refresh_dropdown/popdown）。"""

    def __init__(self, panel):
        self._panel = panel

    def refresh_dropdown(self):
        try:
            items = self._panel._history_summaries_json()
            current_id = self._panel._ai_conversation_id or ""
            js = "renderHistoryList(%s, %s);" % (
                items,
                json.dumps(current_id, ensure_ascii=False),
            )
            self._run_webview_js(js)
        except Exception:
            pass

    def popdown(self):
        self._run_webview_js("hideHistoryDropdown();")
