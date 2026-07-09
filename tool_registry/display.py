"""Display formatting — HTML snippets for WebView tool call display."""

import html
import json
from typing import Any, Dict, List


def _make_collapsible_preview(content: str, label: str, max_chars: int = 500,
                              use_pre: bool = False) -> str:
    """Build a collapsible preview HTML block."""
    truncated = len(content) > max_chars
    preview = content[:max_chars]
    if truncated:
        preview += f"\n\n...（已截断，共 {len(content)} 字符）"
    inner = html.escape(preview)
    return (
        f'<div class="tool-result-box">\n'
        f'<div class="tool-result-header">\n'
        f'<span>📄 {html.escape(label)}</span>\n'
        f'<span class="tool-result-toggle" onclick="toggleToolResult(this)">展开</span>\n'
        f'</div>\n'
        f'<pre class="tool-result-content" style="display: none;">\n'
        f'{inner}\n'
        f'</pre>\n'
        f'</div><!-- tool-result-marker -->'
    )


def format_tool_calls_for_display(tool_calls: List[dict]) -> str:
    """Format tool calls into an HTML snippet for WebView display."""
    if not tool_calls:
        return ""

    parts = []
    for tc in tool_calls:
        name = tc.get("function", {}).get("name", "")
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}

        if name == "web_search":
            query = args.get("query", "")
            safe_query = html.escape(query)
            parts.append(f'<div class="tool-call-info">🔍 <b>网络搜索：</b>{safe_query}</div>')
        elif name == "web_fetch":
            url = args.get("url", "")
            safe_url = html.escape(url)
            parts.append(f'<div class="tool-call-info">📄 <b>获取页面：</b>{safe_url}</div>')
        elif name == "list_directory":
            path = args.get("path", "")
            safe_path = html.escape(path)
            parts.append(f'<div class="tool-call-info">📁 <b>列出目录：</b>{safe_path}</div>')
        elif name == "read_file":
            path = args.get("path", "")
            start = args.get("start_line", 1)
            end = args.get("end_line")
            safe_path = html.escape(path)
            if start > 1 or end is not None:
                range_str = f"第 {start} 行至 {end if end is not None else '末尾'}"
                parts.append(f'<div class="tool-call-info">📝 <b>读取文件：</b>{safe_path} ({range_str})</div>')
            else:
                parts.append(f'<div class="tool-call-info">📝 <b>读取文件：</b>{safe_path}</div>')
        elif name == "get_current_time":
            tz = args.get("timezone", "")
            if tz:
                safe_tz = html.escape(tz)
                parts.append(f'<div class="tool-call-info">🕐 <b>查询时间：</b>{safe_tz}</div>')
            else:
                parts.append('<div class="tool-call-info">🕐 <b>查询时间：</b>本地时间</div>')
        elif name == "grep_search":
            pattern = args.get("pattern", "")
            search_path = args.get("path", "")
            safe_pattern = html.escape(pattern)
            safe_path = html.escape(search_path)
            parts.append(f'<div class="tool-call-info">🔍 <b>搜索内容：</b>{safe_pattern} 在 {safe_path}</div>')
        elif name == "glob_find":
            gpattern = args.get("pattern", "")
            gpath = args.get("path", "")
            safe_gpattern = html.escape(gpattern)
            safe_gpath = html.escape(gpath)
            parts.append(f'<div class="tool-call-info">📂 <b>查找文件：</b>{safe_gpattern} 在 {safe_gpath}</div>')
        elif name == "file_info":
            fpath = args.get("path", "")
            safe_fpath = html.escape(fpath)
            parts.append(f'<div class="tool-call-info">📋 <b>文件信息：</b>{safe_fpath}</div>')
        elif name == "ask_user_question":
            parts.append('<div class="tool-call-info">💬 <b>询问用户</b></div>')
        elif name == "write_file":
            wpath = args.get("path", "")
            content = args.get("content", "")
            safe_wpath = html.escape(wpath)
            mode = "覆盖" if args.get("force", False) else "写入"
            preview_label = f"内容预览（{len(content)} 字符）" if len(content) <= 500 else f"内容预览（前500 / 共{len(content)} 字符）"
            parts.append(
                f'<div class="tool-call-info">✏️ <b>{mode}文件：</b>{safe_wpath}</div>'
                + _make_collapsible_preview(content, preview_label)
            )
        elif name == "edit_file":
            epath = args.get("path", "")
            eold = args.get("old_string", "")
            re_all = args.get("replace_all", False)
            safe_epath = html.escape(epath)
            old_preview = html.escape(eold[:200])
            old_label = f"替换原文（{len(eold)} 字符）" if len(eold) <= 200 else f"替换原文（前200 / 共{len(eold)} 字符）"
            plural = "全部" if re_all else "第一处"
            parts.append(
                f'<div class="tool-call-info">✏️ <b>编辑文件（{plural}匹配）：</b>{safe_epath}</div>'
                + _make_collapsible_preview(eold, old_label, max_chars=200)
            )
        elif name == "delete_file":
            dpath = args.get("path", "")
            rec = args.get("recursive", False)
            safe_dpath = html.escape(dpath)
            rec_tag = "（递归）" if rec else ""
            parts.append(f'<div class="tool-call-info">🗑️ <b>删除{rec_tag}：</b>{safe_dpath}</div>')
        elif name == "rename_file":
            rsrc = args.get("source", "")
            rdst = args.get("destination", "")
            safe_rsrc = html.escape(rsrc)
            safe_rdst = html.escape(rdst)
            parts.append(f'<div class="tool-call-info">📦 <b>重命名：</b>{safe_rsrc} → {safe_rdst}</div>')
        elif name == "todo_create":
            ttitle = args.get("title", "")
            tpriority = args.get("priority", "medium")
            safe_title = html.escape(ttitle)
            parts.append(
                f'<div class="tool-call-info">✅ <b>创建任务：</b>{safe_title}'
                f'<span style="color:#888;font-size:11px;margin-left:8px;">优先级: {tpriority}</span></div>'
            )
        elif name == "todo_update":
            tid = args.get("id", "")
            tstatus = args.get("status", "")
            safe_id = html.escape(tid)
            parts.append(
                f'<div class="tool-call-info">🔄 <b>更新任务：</b>{safe_id}'
                + (f' → {tstatus}' if tstatus else '') + '</div>'
            )
        elif name == "todo_list":
            tid = args.get("id", "")
            sfilter = args.get("status_filter", "")
            if tid:
                parts.append(f'<div class="tool-call-info">📋 <b>查询任务：</b>{html.escape(tid)}</div>')
            elif sfilter:
                parts.append(f'<div class="tool-call-info">📋 <b>任务清单：</b>仅 {sfilter}</div>')
            else:
                parts.append('<div class="tool-call-info">📋 <b>任务清单：</b>全部</div>')
        elif name == "send_notification":
            nsummary = args.get("summary", "")
            nurgency = args.get("urgency", "normal")
            safe_nsum = html.escape(nsummary)
            parts.append(
                f'<div class="tool-call-info">🔔 <b>发送通知：</b>{safe_nsum}'
                f'<span style="color:#888;font-size:11px;margin-left:8px;">紧急度: {nurgency}</span></div>'
            )
        elif name == "sub_agent":
            stask = args.get("task", "")
            safe_task = html.escape(stask[:120])
            max_t = args.get("max_turns", 10)
            max_tok = args.get("max_tokens")
            tok_str = f"，最大 Token: {max_tok}" if max_tok is not None else ""
            parts.append(
                f'<div class="tool-call-info">🔄 <b>子代理任务：</b>{safe_task}'
                f'<span style="color:#888;font-size:11px;margin-left:8px;">最多 {max_t} 轮{tok_str}</span></div>'
            )
        elif name == "bash":
            cmd = args.get("command", "")
            cmd_timeout = args.get("timeout", 60)
            safe_cmd = html.escape(cmd)
            first_line = cmd.split("\n")[0].strip() if cmd else ""
            safe_first = html.escape(first_line)
            cmd_label = f"命令预览（{len(cmd)} 字符）" if len(cmd) <= 300 else f"命令预览（前300 / 共{len(cmd)} 字符）"
            parts.append(
                f'<div class="tool-call-info">🖥️ <b>执行命令：</b>{safe_first}</div>'
                f'<div style="margin: 2px 0 4px 16px; font-size: 11px; color: #888;">超时限制：{cmd_timeout}s</div>'
                + _make_collapsible_preview(cmd, cmd_label, max_chars=300, use_pre=True)
            )
        elif name == "read_qq_mail":
            count = args.get("max_results", 5)
            folder = args.get("folder", "INBOX")
            criteria = args.get("search_criteria", "ALL")
            safe_folder = html.escape(folder)
            safe_criteria = html.escape(criteria)
            parts.append(
                f'<div class="tool-call-info">📧 <b>读取QQ邮件：</b>'
                f'{count} 封，文件夹: {safe_folder}，条件: {safe_criteria}</div>'
            )
        elif name == "parse_file_ast":
            fpath = args.get("path", "")
            lang = args.get("language", "auto")
            safe_path = html.escape(fpath)
            safe_lang = html.escape(lang)
            parts.append(f'<div class="tool-call-info">📦 <b>解析AST：</b>{safe_path} ({safe_lang})</div>')
        elif name == "get_code_metrics":
            fpath = args.get("path", "")
            safe_path = html.escape(fpath)
            parts.append(f'<div class="tool-call-info">📊 <b>代码度量：</b>{safe_path}</div>')
        elif name == "find_project_dependencies":
            fpath = args.get("path", "")
            rec = args.get("recursive", True)
            safe_path = html.escape(fpath)
            rec_str = "递归" if rec else "非递归"
            parts.append(f'<div class="tool-call-info">🔗 <b>项目依赖分析：</b>{safe_path} ({rec_str})</div>')
        else:
            safe_name = html.escape(name)
            parts.append(f'<div class="tool-call-info">🔧 <b>工具调用：</b>{safe_name}</div>')

    return "\n".join(parts)


def render_collapsible_tool_result(name: str, content: str) -> str:
    """Render tool result block into collapsible HTML structure."""
    safe_name = html.escape(name)
    MAX_TOOL_DISPLAY = 2000
    display = content[:MAX_TOOL_DISPLAY]
    if len(content) > MAX_TOOL_DISPLAY:
        display += f"\n\n...（结果已截断，共 {len(content)} 字符）"
    safe_display = html.escape(display)

    return (
        f'<div class="tool-result-box">\n'
        f'<div class="tool-result-header">\n'
        f'<span>📎 工具结果 ({safe_name})</span>\n'
        f'<span class="tool-result-toggle" onclick="toggleToolResult(this)">展开</span>\n'
        f'</div>\n'
        f'<pre class="tool-result-content" style="display: none;">\n'
        f'{safe_display}\n'
        f'</pre>\n'
        f'</div><!-- tool-result-marker -->'
    )


def format_tool_result_for_display(name: str, content: str) -> str:
    """Format a tool execution result into an HTML snippet for WebView display."""
    return render_collapsible_tool_result(name, content)
