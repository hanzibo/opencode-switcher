"""Subagent monitoring and status bar mixin for AIChatPanel."""

import sys
from typing import Optional, Any

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


class SubagentMixin:
    """子代理状态监控、FlowBox 状态栏动态渲染与交互 Mixin。"""

    def _on_subagent_status_changed(self, sid: str, info: Optional[dict]):
        """Event-driven callback triggered when a subagent's status changes."""
        try:
            active_conv_id = self._ai_conversation_id
            
            # If info is None, it represents a deletion event
            if info is None:
                self._remove_subagent_block(sid)
                self._update_subagent_bar_visibility()
                return

            # Check if this subagent belongs to the active conversation
            if info.get("conv_id") != active_conv_id:
                return

            status = info.get("status")
            if status == "removed":
                self._remove_subagent_block(sid)
            else:
                if sid in self._ai_subagent_blocks:
                    self._update_subagent_block(sid, info)
                else:
                    self._create_subagent_block(sid, info)
            
            self._update_subagent_bar_visibility()
        except Exception as e:
            print(f"[opencode-switcher] error in _on_subagent_status_changed: {e}", file=sys.stderr)

    def _refresh_subagent_bar(self):
        """Clear and rebuild subagent status blocks for the active conversation."""
        try:
            self._clear_subagent_bar_instantly()
            from tool_registry import get_subagent_status_map
            status_map = get_subagent_status_map()
            active_conv_id = self._ai_conversation_id
            
            for sid, info in status_map.items():
                if info.get("conv_id") == active_conv_id:
                    self._create_subagent_block(sid, info)
                    
            self._update_subagent_bar_visibility()
        except Exception as e:
            print(f"[opencode-switcher] error in _refresh_subagent_bar: {e}", file=sys.stderr)

    def _build_subagent_tooltip(self, sid: Any, info: dict) -> str:
        """构建子代理状态块浮窗文本（悬停显示，含轮次/工具计数与工具历史）。

        工具名来自函数名，无 HTML 注入风险；使用纯文本 set_tooltip_text。
        """
        status = info.get("status", "unknown")
        action = info.get("action", "")
        turn = info.get("turn", 0)
        tool_count = info.get("tool_calls_count", 0)
        if status == "running":
            status_line = "状态：运行中 🔄"
        elif status == "completed":
            status_line = "状态：已完成 ✅"
        else:
            status_line = "状态：失败 ❌"
        lines = [
            f"子代理 {sid}",
            status_line,
        ]
        if status == "running":
            lines.append(f"动作：{action or 'Thinking'}")
        lines.append(f"轮次：第 {turn} 轮")
        lines.append(f"工具调用：{tool_count} 次")
        lines.append("── 最近工具 ──")
        hist = info.get("tools_history") or []
        if hist:
            lines.extend(f"{i}. {name}" for i, name in enumerate(hist, 1))
        else:
            lines.append("（暂无）")
        return "\n".join(lines)

    def _create_subagent_block(self, sid: Any, info: dict):
        """Create a FlowBoxChild for a sub-agent status block."""
        child = Gtk.FlowBoxChild.new()
        box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)

        # 进度感知：运行中左侧持续旋转的加载圈（A 方案）
        spinner = Gtk.Spinner.new()
        spinner.get_style_context().add_class("subagent-spinner")
        spinner.set_no_show_all(True)
        box.pack_start(spinner, False, False, 0)

        local_id = sid.split("-")[-1] if isinstance(sid, str) and "-" in sid else sid
        status = info.get("status", "unknown")
        turn = info.get("turn", 0)
        tool_count = info.get("tool_calls_count", 0)

        if status == "running":
            # 轮次/工具计数（A+B 方案）：第 N 轮每轮必变，工具×M 单调递增
            spinner.set_no_show_all(False)
            label_text = f"子代理 {local_id} · 第 {turn} 轮 · 工具×{tool_count}"
            spinner.start()
        else:
            label_text = f"子代理 {local_id}"
            spinner.stop()
            spinner.hide()

        label = Gtk.Label.new(label_text)
        label.set_margin_start(4)
        label.set_margin_end(4)
        label.set_margin_top(2)
        label.set_margin_bottom(2)
        box.pack_start(label, True, True, 0)
        child.add(box)

        tooltip_text = self._build_subagent_tooltip(sid, info)
        box_ctx = box.get_style_context()
        if status == "completed":
            box_ctx.add_class("subagent-block-done")
        elif status == "running":
            box_ctx.add_class("subagent-block-running")
        else:
            box_ctx.add_class("subagent-block-failed")
        child.set_tooltip_text(tooltip_text)

        self._ai_subagent_bar.add(child)
        self._ai_subagent_blocks[sid] = (child, child, box, spinner)
        self._ai_subagent_bar.show_all()

    def _update_subagent_block(self, sid: Any, info: dict):
        """Update an existing block when sub-agent status changes."""
        entry = self._ai_subagent_blocks.get(sid)
        if entry is None:
            return
        child, event_box, box, spinner = entry
        status = info.get("status", "unknown")
        action = info.get("action", "")
        turn = info.get("turn", 0)
        tool_count = info.get("tool_calls_count", 0)
        local_id = sid.split("-")[-1] if isinstance(sid, str) and "-" in sid else sid
        ctx = box.get_style_context()

        # 更新标签文本（box 内第一个 child 是 spinner，其后是 Label）
        lbl = next((w for w in box.get_children() if isinstance(w, Gtk.Label)), None)
        if lbl is not None:
            if status == "running":
                lbl.set_text(f"子代理 {local_id} · 第 {turn} 轮 · 工具×{tool_count}")
            else:
                lbl.set_text(f"子代理 {local_id}")

        # 更新 spinner 生命周期：running 持续旋转，终态停止并隐藏
        if status == "running":
            spinner.set_no_show_all(False)
            spinner.show()
            spinner.start()
        else:
            spinner.stop()
            spinner.hide()

        tooltip_text = self._build_subagent_tooltip(sid, info)
        if status == "completed":
            ctx.remove_class("subagent-block-running")
            ctx.add_class("subagent-block-done")
            event_box.set_tooltip_text(tooltip_text)
        elif status == "running":
            if ctx.has_class("subagent-block-done"):
                ctx.remove_class("subagent-block-done")
                self._ai_selected_subagents.discard(sid)
            ctx.add_class("subagent-block-running")
            event_box.set_tooltip_text(tooltip_text)
        else:
            ctx.remove_class("subagent-block-running")
            ctx.add_class("subagent-block-failed")
            event_box.set_tooltip_text(tooltip_text)

    def _remove_subagent_block(self, sid: Any):
        """Remove a sub-agent block and clean up state."""
        self._ai_selected_subagents.discard(sid)
        entry = self._ai_subagent_blocks.pop(sid, None)
        if not entry:
            return
        child, _event_box, _box, spinner = entry
        spinner.stop()
        if child.get_parent() is not None:
            self._ai_subagent_bar.remove(child)
        if not self._ai_subagent_blocks:
            self._ai_subagent_bar.get_style_context().remove_class("subagent-status-bar")
            self._ai_subagent_bar.hide()

    def _clear_subagent_bar_instantly(self):
        """Instantly clear all subagent blocks from the status bar UI."""
        self._ai_subagent_bar.get_style_context().remove_class("subagent-status-bar")
        self._ai_subagent_bar.hide()
        for _sid, entry in self._ai_subagent_blocks.items():
            entry[3].stop()
        for child in self._ai_subagent_bar.get_children():
            self._ai_subagent_bar.remove(child)
        self._ai_subagent_blocks.clear()
        self._ai_selected_subagents.clear()
        self._update_subagent_bar_visibility()

    def _update_subagent_bar_visibility(self):
        """Show or hide the subagent bar based on whether any blocks exist."""
        has_blocks = len(self._ai_subagent_blocks) > 0
        if has_blocks:
            self._ai_subagent_bar.get_style_context().add_class("subagent-status-bar")
            self._ai_subagent_bar.set_no_show_all(False)
            self._ai_subagent_bar.show_all()
        else:
            self._ai_subagent_bar.get_style_context().remove_class("subagent-status-bar")
            self._ai_subagent_bar.hide()
            self._ai_subagent_bar.set_no_show_all(True)

    def _on_subagent_block_click(self, sid: Any):
        """Toggle selection state of a completed sub-agent block."""
        entry = self._ai_subagent_blocks.get(sid)
        if entry is None:
            return True
        child, event_box, box, _spinner = entry
        from tool_registry import get_subagent_status_map
        info = get_subagent_status_map().get(sid, {})
        if info.get("status") != "completed":
            return True
        ctx = box.get_style_context()
        if sid in self._ai_selected_subagents:
            self._ai_selected_subagents.discard(sid)
            ctx.remove_class("subagent-block-selected")
        else:
            self._ai_selected_subagents.add(sid)
            ctx.add_class("subagent-block-selected")
        return True

    def _on_subagent_child_activated(self, flowbox, child):
        """Handle child activation signal from FlowBox to toggle selection."""
        sid = None
        for k, v in self._ai_subagent_blocks.items():
            if v[0] == child:
                sid = k
                break
        if sid is not None:
            self._on_subagent_block_click(sid)
