"""AI configuration tab (truncation, summary, skills, and built-in tools)."""

import html
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango


class AITabMixin:
    """AI 对话截断、摘要、Skills 及内置工具开关设置标签页。"""

    # ── Tab: AI 对话 ───────────────────────────────────────────────────

    def _build_ai_settings_tab(self):
        """Build the AI conversation truncation settings tab page.

        Returns a Gtk.ScrolledWindow ready for notebook.append_page().
        """
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)

        # ── Soft limit (triggering threshold) ──
        soft_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        soft_lbl = Gtk.Label.new("触发截断的消息数:")
        soft_lbl.set_size_request(150, -1)
        soft_lbl.set_xalign(0)
        self._soft_spin = Gtk.SpinButton.new_with_range(50, 9999, 10)
        self._soft_spin.set_value(self._ai_settings_store.soft_limit)
        soft_hbox.pack_start(soft_lbl, False, False, 0)
        soft_hbox.pack_start(self._soft_spin, False, False, 0)
        vbox.pack_start(soft_hbox, False, False, 0)

        # ── Trim target ──
        trim_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        trim_lbl = Gtk.Label.new("裁剪后保留的消息数:")
        trim_lbl.set_size_request(150, -1)
        trim_lbl.set_xalign(0)
        self._trim_spin = Gtk.SpinButton.new_with_range(10, 400, 10)
        self._trim_spin.set_value(self._ai_settings_store.trim_target)
        trim_hbox.pack_start(trim_lbl, False, False, 0)
        trim_hbox.pack_start(self._trim_spin, False, False, 0)
        vbox.pack_start(trim_hbox, False, False, 0)

        # ── Help text ──
        hint = Gtk.Label.new()
        hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "当消息数超过「触发截断的消息数」时，自动裁剪到「裁剪后保留的消息数」。\n"
            "首条消息始终保留，从最旧的开始丢弃。"
            "</span>"
        )
        hint.set_xalign(0)
        hint.set_margin_top(12)
        vbox.pack_start(hint, False, False, 0)

        # ── Separator before summary compression settings ──
        sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(16)
        sep.set_margin_bottom(12)
        vbox.pack_start(sep, False, False, 0)

        # ── Summary compression section title ──
        summary_title = Gtk.Label.new()
        summary_title.set_markup("<b>📝 摘要压缩</b>")
        summary_title.set_xalign(0)
        vbox.pack_start(summary_title, False, False, 0)

        # ── Enable summary compression ──
        summary_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        summary_hbox.set_margin_top(8)
        self._enable_summary_check = Gtk.CheckButton.new_with_label("启用摘要压缩")
        self._enable_summary_check.set_active(self._ai_settings_store.enable_summary)
        summary_hbox.pack_start(self._enable_summary_check, False, False, 0)
        vbox.pack_start(summary_hbox, False, False, 0)

        # ── Summary threshold ──
        thresh_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        thresh_lbl = Gtk.Label.new("触发摘要的消息余量:")
        thresh_lbl.set_size_request(150, -1)
        thresh_lbl.set_xalign(0)
        self._summary_thresh_spin = Gtk.SpinButton.new_with_range(20, 300, 10)
        self._summary_thresh_spin.set_value(self._ai_settings_store.summary_threshold)
        thresh_hbox.pack_start(thresh_lbl, False, False, 0)
        thresh_hbox.pack_start(self._summary_thresh_spin, False, False, 0)
        vbox.pack_start(thresh_hbox, False, False, 0)

        # ── Summary max chars ──
        max_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        max_lbl = Gtk.Label.new("摘要最大字符数:")
        max_lbl.set_size_request(150, -1)
        max_lbl.set_xalign(0)
        self._summary_max_spin = Gtk.SpinButton.new_with_range(100, 2000, 100)
        self._summary_max_spin.set_value(self._ai_settings_store.summary_max_chars)
        max_hbox.pack_start(max_lbl, False, False, 0)
        max_hbox.pack_start(self._summary_max_spin, False, False, 0)
        vbox.pack_start(max_hbox, False, False, 0)

        # ── Summary prompt template ──
        prompt_lbl = Gtk.Label.new("摘要提示词模板（支持占位符）:")
        prompt_lbl.set_xalign(0)
        prompt_lbl.set_margin_top(12)
        vbox.pack_start(prompt_lbl, False, False, 0)

        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_min_content_height(120)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_shadow_type(Gtk.ShadowType.NONE)
        self._summary_prompt_view = Gtk.TextView.new()
        self._summary_prompt_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._summary_prompt_view.set_monospace(True)
        buffer = self._summary_prompt_view.get_buffer()
        buffer.set_text(self._ai_settings_store.summary_prompt_template)
        scrolled.add(self._summary_prompt_view)
        vbox.pack_start(scrolled, False, False, 0)

        prompt_hint = Gtk.Label.new()
        prompt_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "可用占位符："
            "{prev_summary} 已有摘要 / "
            "{conversation_text} 对话内容 / "
            "{max_chars} 最大字符数"
            "</span>"
        )
        prompt_hint.set_xalign(0)
        prompt_hint.set_margin_top(4)
        vbox.pack_start(prompt_hint, False, False, 0)

        # ── Help text for summary ──
        summary_hint = Gtk.Label.new()
        summary_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "启用后，当消息数超过阈值时，将最早的消息压缩为摘要而不是直接丢弃，"
            "保留关键信息。\n摘要会作为系统消息注入后续对话，帮助 Agent 记住早期内容。"
            "</span>"
        )
        summary_hint.set_xalign(0)
        summary_hint.set_margin_top(8)
        vbox.pack_start(summary_hint, False, False, 0)

        # ── Separator before code highlight ──
        hl_sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        hl_sep.set_margin_top(16)
        hl_sep.set_margin_bottom(12)
        vbox.pack_start(hl_sep, False, False, 0)

        # ── Code highlight section title ──
        hl_title = Gtk.Label.new()
        hl_title.set_markup("<b>🎨 代码渲染</b>")
        hl_title.set_xalign(0)
        vbox.pack_start(hl_title, False, False, 0)

        # ── Enable code highlight ──
        hl_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        hl_hbox.set_margin_top(8)
        self._code_highlight_check = Gtk.CheckButton.new_with_label("启用代码语法高亮（Pygments）")
        self._code_highlight_check.set_active(self._ai_settings_store.enable_code_highlight)
        hl_hbox.pack_start(self._code_highlight_check, False, False, 0)
        vbox.pack_start(hl_hbox, False, False, 0)

        hl_hint = Gtk.Label.new()
        hl_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "关闭后可降低渲染开销，对设备性能较弱的场景有明显改善。"
            "需要 Python 包 Pygments 支持。"
            "</span>"
        )
        hl_hint.set_xalign(0)
        hl_hint.set_margin_top(4)
        vbox.pack_start(hl_hint, False, False, 0)

        # ── Separator before Skill settings ──
        skill_sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        skill_sep.set_margin_top(16)
        skill_sep.set_margin_bottom(12)
        vbox.pack_start(skill_sep, False, False, 0)

        # ── Skill section title ──
        skill_title = Gtk.Label.new()
        skill_title.set_markup("<b>🎯 AI Skill 技能系统</b>")
        skill_title.set_xalign(0)
        vbox.pack_start(skill_title, False, False, 0)

        # ── Enable global skills CheckButton ──
        skill_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        skill_hbox.set_margin_top(8)
        self._enable_global_skills_check = Gtk.CheckButton.new_with_label("启用全局 Skill 自动发现 (~/.config/opencode-switcher/skills, ~/.agents/skills 等)")
        self._enable_global_skills_check.set_active(self._ai_settings_store.enable_global_skills)
        skill_hbox.pack_start(self._enable_global_skills_check, False, False, 0)
        vbox.pack_start(skill_hbox, False, False, 0)

        skill_hint = Gtk.Label.new()
        skill_hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "关闭后 AI 助手将仅自动识别当前项目根路径（.opencode/skills 及 .gemini/skills）下的 Skill，"
            "屏蔽全局通用 Skill 目录。\n下方可对已识别到的每个 Skill 进行单独开启/关闭管理。"
            "</span>"
        )
        skill_hint.set_xalign(0)
        skill_hint.set_margin_top(4)
        vbox.pack_start(skill_hint, False, False, 0)

        # ── Per-Skill enable/disable toggle list ──
        self._build_skill_toggle_section(vbox)

        # ── Separator before built-in tool toggle ──
        tool_sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        tool_sep.set_margin_top(16)
        tool_sep.set_margin_bottom(12)
        vbox.pack_start(tool_sep, False, False, 0)

        # ── Built-in tool toggle section ──
        self._build_tool_toggle_section(vbox)

        # ── Spacer ──
        spacer = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        spacer.set_vexpand(True)
        vbox.pack_start(spacer, True, True, 0)

        return self._make_tab_scrolled_window(vbox)

    def _build_skill_toggle_section(self, parent_vbox):
        """动态渲染每个已扫描到的 Skill 的独立使能开关。"""
        from stores.skill_store import SkillStore
        from tool_registry import get_bash_cwd

        cwd = get_bash_cwd()
        all_skills = SkillStore(disabled_skills=[]).get_skills(cwd=cwd)

        self._skill_toggle_widgets = []
        disabled_set = set(self._ai_settings_store.disabled_skills)

        if not all_skills:
            no_skills_lbl = Gtk.Label.new("（当前未扫描到任何已安装的 Skill）")
            no_skills_lbl.set_xalign(0)
            no_skills_lbl.set_opacity(0.6)
            no_skills_lbl.set_margin_top(6)
            parent_vbox.pack_start(no_skills_lbl, False, False, 0)
            return

        skill_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 4)
        skill_box.set_margin_top(8)

        global_dirs = SkillStore().global_dirs

        for sk in all_skills:
            hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
            check = Gtk.CheckButton.new_with_label(sk.name)
            check.set_active(sk.name not in disabled_set)

            is_global = any(sk.path.startswith(g) for g in global_dirs if g)
            tag_text = "[全局]" if is_global else "[项目]"
            tag_color = "#3b82f6" if is_global else "#10b981"

            desc_lbl = Gtk.Label.new()
            desc_lbl.set_markup(
                f"<span foreground='{tag_color}'><b>{tag_text}</b></span> "
                f"<span foreground='#888888'>{html.escape(sk.description)}</span>"
            )
            desc_lbl.set_xalign(0)
            desc_lbl.set_ellipsize(Pango.EllipsizeMode.END)

            hbox.pack_start(check, False, False, 0)
            hbox.pack_start(desc_lbl, True, True, 0)
            skill_box.pack_start(hbox, False, False, 0)

            self._skill_toggle_widgets.append({
                "name": sk.name,
                "check": check,
            })

        parent_vbox.pack_start(skill_box, False, False, 0)

    def _build_tool_toggle_section(self, parent_vbox):
        """Build the built-in tool enable/disable toggle section.

        按模块分组显示所有内置工具，每组可折叠，每个工具有独立开关。
        """
        from tool_registry import TOOL_DEFINITIONS, TOOL_MODULE_MAP

        # ── Section title ──
        title = Gtk.Label.new()
        title.set_markup("<b>🔧 内置工具开关</b>")
        title.set_xalign(0)
        parent_vbox.pack_start(title, False, False, 0)

        hint = Gtk.Label.new()
        hint.set_markup(
            "<span size='small' foreground='#888888'>"
            "取消勾选可禁用对应内置工具，AI 将无法调用。"
            "MCP 工具不受此设置影响。"
            "</span>"
        )
        hint.set_xalign(0)
        hint.set_margin_top(4)
        parent_vbox.pack_start(hint, False, False, 0)

        # ── 解析工具按模块分组 ──
        groups: dict = {}
        risk_map = {
            "bash": "high", "write_file": "high", "edit_file": "high", "sub_agent": "high",
            "read_file": "medium", "grep_search": "medium",
            "web_search": "medium", "web_fetch": "medium",
            "read_qq_mail": "medium", "read_gmail_mail": "medium", "memory_save": "medium",
        }

        for s in TOOL_DEFINITIONS:
            name = s["function"]["name"]
            group = TOOL_MODULE_MAP.get(name, "other")
            groups.setdefault(group, []).append(s)

        # 组排序（按重要性）
        group_order = ["common", "todo", "filesystem", "search", "web", "bash",
                       "notification", "mail", "subagent", "code_analysis",
                       "memory", "skill"]
        group_labels = {
            "common": "🟢 通用", "todo": "🟢 任务管理",
            "filesystem": "🔴 文件系统", "search": "🟡 搜索",
            "web": "🟡 网页", "bash": "🔴 Shell",
            "notification": "🟢 通知", "mail": "🟡 邮件",
            "subagent": "🔴 子代理", "code_analysis": "🟢 代码分析",
            "memory": "🟡 记忆", "skill": "🟡 技能",
        }

        # 存储所有 checkbutton 以便保存时读取
        self._tool_toggle_widgets: list[dict] = []
        self._tool_toggle_lookup: dict[str, Gtk.CheckButton] = {}
        disabled_set = set(self._ai_settings_store.disabled_tools)

        # 每组一个可折叠区域
        expander = Gtk.Expander.new("全部工具 (点击展开/收起)")
        expander.set_expanded(False)
        expander.set_margin_top(8)

        inner_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 4)
        inner_box.set_margin_start(12)

        for g in group_order:
            schemas = groups.get(g, [])
            if not schemas:
                continue

            # ── 组标题（不可折叠，仅用于视觉分组） ──
            group_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
            group_box.set_margin_top(8)

            group_label_text = group_labels.get(g, g)
            group_lbl = Gtk.Label.new()
            group_lbl.set_markup(f"<b>{group_label_text}</b>")
            group_lbl.set_xalign(0)
            group_lbl.set_size_request(120, -1)
            group_box.pack_start(group_lbl, False, False, 0)

            # 全选/全不选按钮
            group_all_in = all(
                s["function"]["name"] not in disabled_set for s in schemas
            )
            group_check = Gtk.CheckButton.new_with_label("全选")
            group_check.set_active(group_all_in)
            group_check.connect("toggled", self._on_group_toggle, schemas, inner_box)
            group_box.pack_start(group_check, False, False, 0)

            inner_box.pack_start(group_box, False, False, 0)

            # ── 每个工具一个 checkbutton ──
            for s in schemas:
                name = s["function"]["name"]
                desc = s["function"]["description"][:60] + ("…" if len(s["function"]["description"]) > 60 else "")

                tool_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
                tool_hbox.set_margin_start(16)
                tool_hbox.set_margin_top(2)

                risk = risk_map.get(name, "low")
                risk_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(risk, "🟢")

                check = Gtk.CheckButton.new()
                check.set_active(name not in disabled_set)
                check.set_tooltip_text(f"{name}: {desc}")
                tool_hbox.pack_start(check, False, False, 0)

                name_lbl = Gtk.Label.new()
                name_lbl.set_markup(f"{risk_icon} {name}")
                name_lbl.set_xalign(0)
                name_lbl.set_size_request(220, -1)
                tool_hbox.pack_start(name_lbl, False, False, 0)

                desc_lbl = Gtk.Label.new()
                desc_lbl.set_markup(f"<span size='small' foreground='#888888'>{desc}</span>")
                desc_lbl.set_xalign(0)
                desc_lbl.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
                desc_lbl.set_size_request(300, -1)
                tool_hbox.pack_start(desc_lbl, False, False, 0)

                inner_box.pack_start(tool_hbox, False, False, 0)

                self._tool_toggle_widgets.append({
                    "name": name,
                    "check": check,
                    "group": g,
                })
                self._tool_toggle_lookup[name] = check

        expander.add(inner_box)
        parent_vbox.pack_start(expander, False, False, 0)

        # ── 底部操作按钮 ──
        btn_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        btn_box.set_margin_top(8)

        enable_all_btn = Gtk.Button.new_with_label("全部启用")
        enable_all_btn.connect("clicked", self._on_tool_enable_all)
        btn_box.pack_start(enable_all_btn, False, False, 0)

        disable_high_btn = Gtk.Button.new_with_label("禁用高风险")
        disable_high_btn.set_tooltip_text("一键禁用 bash、write_file、edit_file、sub_agent")
        disable_high_btn.connect("clicked", self._on_tool_disable_high_risk)
        btn_box.pack_start(disable_high_btn, False, False, 0)

        parent_vbox.pack_start(btn_box, False, False, 0)

    def _on_group_toggle(self, btn, schemas, inner_box):
        """组全选/全不选按钮切换时，同步组内所有工具。"""
        active = btn.get_active()
        for s in schemas:
            check = self._tool_toggle_lookup.get(s["function"]["name"])
            if check:
                check.set_active(active)

    def _on_tool_enable_all(self, btn):
        """全部启用按钮。"""
        for w in self._tool_toggle_widgets:
            w["check"].set_active(True)

    def _on_tool_disable_high_risk(self, btn):
        """禁用高风险工具。"""
        high_risk = {"bash", "write_file", "edit_file", "sub_agent"}
        for w in self._tool_toggle_widgets:
            if w["name"] in high_risk:
                w["check"].set_active(False)
            else:
                w["check"].set_active(True)
