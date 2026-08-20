"""MCP Server configuration tab (Master-Detail layout)."""

import threading
from typing import Optional

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

from mcp_integration import MCPServerConfig, MCPClientManager


class MCPTabMixin:
    """MCP 服务器 Master-Detail 左右双栏管理标签页。"""

    # ── Tab: MCP 服务器 (Master-Detail 左右分栏布局) ──────────────────────

    def _build_mcp_tab(self):
        """Build the MCP Server configuration tab page with a modern Master-Detail split layout.

        支持 stdio（本地子进程）和 http（远程 Streamable HTTP）两种模式。
        左侧为 MCP 服务器导航列表，右侧为当前选中的具体配置表单与测试面板。
        """
        main_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)

        # ── 左侧列表概览栏 (Master Sidebar) ──
        sidebar_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        sidebar_box.set_size_request(230, -1)
        sidebar_box.set_margin_start(14)
        sidebar_box.set_margin_end(8)
        sidebar_box.set_margin_top(12)
        sidebar_box.set_margin_bottom(12)

        # 顶部常驻「＋ 添加」按钮
        add_btn = Gtk.Button.new_with_label("＋ 添加 MCP 服务器")
        add_btn.set_tooltip_text("添加一个新的 MCP 服务器配置")
        add_btn.connect("clicked", lambda _: self._add_mcp_server_card(select_new=True))
        sidebar_box.pack_start(add_btn, False, False, 0)

        # 列表滚动视口
        list_scrolled = Gtk.ScrolledWindow.new()
        list_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scrolled.set_shadow_type(Gtk.ShadowType.IN)
        list_scrolled.set_vexpand(True)

        self._mcp_list_box = Gtk.ListBox.new()
        self._mcp_list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._mcp_list_box.connect("row-selected", self._on_mcp_row_selected)
        list_scrolled.add(self._mcp_list_box)
        sidebar_box.pack_start(list_scrolled, True, True, 0)

        # 底部状态计数信息
        self._mcp_count_label = Gtk.Label.new()
        self._mcp_count_label.set_xalign(0)
        self._mcp_count_label.set_markup("<span size='small' foreground='#888888'>共 0 个服务器</span>")
        sidebar_box.pack_start(self._mcp_count_label, False, False, 0)

        main_box.pack_start(sidebar_box, False, False, 0)

        # ── 垂直分割线 ──
        sep = Gtk.Separator.new(Gtk.Orientation.VERTICAL)
        main_box.pack_start(sep, False, False, 0)

        # ── 右侧详情编辑区 (Detail Stack) ──
        detail_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        detail_box.set_hexpand(True)
        detail_box.set_vexpand(True)
        detail_box.set_margin_start(12)
        detail_box.set_margin_end(16)
        detail_box.set_margin_top(12)
        detail_box.set_margin_bottom(12)

        self._mcp_stack = Gtk.Stack.new()
        self._mcp_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._mcp_stack.set_transition_duration(150)
        self._mcp_stack.set_hexpand(True)
        self._mcp_stack.set_vexpand(True)

        # 空状态占位页
        empty_view = self._create_mcp_empty_view()
        self._mcp_stack.add_named(empty_view, "empty")
        detail_box.pack_start(self._mcp_stack, True, True, 0)

        main_box.pack_start(detail_box, True, True, 0)

        # ── 加载已有服务器 ──
        self._mcp_server_widgets = []
        self._mcp_item_seq = 0
        existing_servers = getattr(self._ai_settings_store, "mcp_servers", [])
        for sd in existing_servers:
            self._add_mcp_server_card(sd, select_new=False)

        # 默认选中首项
        first_row = self._mcp_list_box.get_row_at_index(0)
        if first_row:
            self._mcp_list_box.select_row(first_row)
        else:
            self._mcp_stack.set_visible_child_name("empty")

        self._update_mcp_count_label()

        return main_box

    def _create_mcp_empty_view(self) -> Gtk.Widget:
        """创建无 MCP 服务器时的占位引导界面。"""
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 12)
        vbox.set_valign(Gtk.Align.CENTER)
        vbox.set_halign(Gtk.Align.CENTER)

        icon_label = Gtk.Label.new()
        icon_label.set_markup("<span size='xx-large' foreground='#888888'>🔌</span>")
        vbox.pack_start(icon_label, False, False, 0)

        title_label = Gtk.Label.new()
        title_label.set_markup("<span size='large' weight='bold'>暂无 MCP 服务器</span>")
        vbox.pack_start(title_label, False, False, 0)

        desc_label = Gtk.Label.new()
        desc_label.set_markup(
            "<span foreground='#888888' size='small'>"
            "MCP (Model Context Protocol) 允许 AI 助手连接本地工具或远程服务。\n"
            "点击左侧「<b>＋ 添加 MCP 服务器</b>」开始配置。"
            "</span>"
        )
        desc_label.set_justify(Gtk.Justification.CENTER)
        vbox.pack_start(desc_label, False, False, 0)

        return vbox

    def _update_mcp_count_label(self):
        """更新侧边栏底部服务器计数。"""
        count = len(self._mcp_server_widgets)
        enabled_count = sum(1 for w in self._mcp_server_widgets if w["enabled"].get_active())
        if hasattr(self, "_mcp_count_label"):
            self._mcp_count_label.set_markup(
                f"<span size='small' foreground='#888888'>共 {count} 个服务器 ({enabled_count} 已启用)</span>"
            )

    def _update_mcp_list_row(self, row: Gtk.ListBoxRow, name: str, enabled: bool, transport: str):
        """轻量级在位更新左侧列表行的状态指示灯、名称及传输徽标。"""
        if not row or not hasattr(row, "_dot_label"):
            return
        dot_color = "#4caf50" if enabled else "#888888"
        row._dot_label.set_markup(f"<span foreground='{dot_color}'>●</span>")
        display_name = name or "(未命名)"
        row._title_label.set_text(display_name)

        is_http = (transport == "http")
        badge_text = "http" if is_http else "stdio"
        badge_color = "#ab47bc" if is_http else "#00897b"
        badge_bg = "#f3e5f5" if is_http else "#e0f2f1"
        row._badge_label.set_markup(
            f"<span size='x-small' weight='bold' foreground='{badge_color}' background='{badge_bg}'> {badge_text} </span>"
        )

    def _on_mcp_row_selected(self, listbox, row):
        """左侧列表选中项变更时切换右侧表单页面。"""
        if row and hasattr(row, "_server_id"):
            self._mcp_stack.set_visible_child_name(row._server_id)
        else:
            if not self._mcp_server_widgets:
                self._mcp_stack.set_visible_child_name("empty")

    @staticmethod
    def _parse_env_str(env_raw: str) -> dict[str, str]:
        """将 KEY=VAL, KEY2=VAL2 格式字符串解析为环境变量字典。"""
        env_dict = {}
        if env_raw:
            for item in env_raw.split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    if k.strip():
                        env_dict[k.strip()] = v.strip()
        return env_dict

    def _add_mcp_server_card(self, data: Optional[dict] = None, select_new: bool = False):
        """添加一个 MCP 服务器配置条目（左侧 ListBoxRow + 右侧 Stack 详情页）。"""
        cfg = MCPServerConfig.from_dict(data or {})
        server_id = f"mcp_server_{self._mcp_item_seq}"
        self._mcp_item_seq += 1

        # ── 1. 创建左侧列表行 (ListBoxRow) ──
        row = Gtk.ListBoxRow.new()
        row._server_id = server_id

        row_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        row_box.set_margin_start(6)
        row_box.set_margin_end(6)
        row_box.set_margin_top(6)
        row_box.set_margin_bottom(6)

        dot_label = Gtk.Label.new()
        row._dot_label = dot_label
        row_box.pack_start(dot_label, False, False, 0)

        title_label = Gtk.Label.new()
        title_label.set_xalign(0)
        title_label.set_hexpand(True)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        row._title_label = title_label
        row_box.pack_start(title_label, True, True, 0)

        badge_label = Gtk.Label.new()
        row._badge_label = badge_label
        row_box.pack_start(badge_label, False, False, 0)

        row.add(row_box)
        row.get_style_context().add_class("mcp-sidebar-row")
        self._update_mcp_list_row(row, cfg.name, cfg.enabled, cfg.transport)
        self._mcp_list_box.add(row)
        row.show_all()

        # ── 2. 创建右侧表单详情页 (Detail Pane in ScrolledWindow) ──
        detail_scrolled = Gtk.ScrolledWindow.new()
        detail_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        detail_scrolled.set_shadow_type(Gtk.ShadowType.NONE)

        detail_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 10)
        detail_vbox.set_margin_start(4)
        detail_vbox.set_margin_end(4)
        detail_vbox.set_margin_top(4)
        detail_vbox.set_margin_bottom(12)
        detail_scrolled.add(detail_vbox)

        # ── 区域 1: 顶部控制卡片 (Name + Enable + Auto-connect + Delete) ──
        header_frame = Gtk.Frame.new()
        header_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        header_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        header_vbox.set_margin_start(10)
        header_vbox.set_margin_end(10)
        header_vbox.set_margin_top(8)
        header_vbox.set_margin_bottom(8)
        header_frame.add(header_vbox)

        row1 = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)

        name_lbl = Gtk.Label.new("名称:")
        name_lbl.set_xalign(0)
        row1.pack_start(name_lbl, False, False, 0)

        name_entry = Gtk.Entry.new()
        name_entry.set_placeholder_text("例如: filesystem 或 github")
        name_entry.set_hexpand(True)
        name_entry.set_text(cfg.name)
        row1.pack_start(name_entry, True, True, 0)

        enabled_check = Gtk.CheckButton.new_with_label("启用")
        enabled_check.set_active(cfg.enabled)
        row1.pack_start(enabled_check, False, False, 0)

        auto_check = Gtk.CheckButton.new_with_label("自动连接")
        auto_check.set_active(cfg.auto_connect)
        row1.pack_start(auto_check, False, False, 0)

        del_btn = Gtk.Button.new_with_label("🗑 删除")
        del_btn.set_tooltip_text("删除此 MCP 服务器")
        del_btn.connect("clicked", lambda _: self._remove_mcp_server_card(server_id))
        row1.pack_start(del_btn, False, False, 0)

        header_vbox.pack_start(row1, False, False, 0)

        # 传输方式选择行
        row_trans = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        trans_lbl = Gtk.Label.new("传输方式:")
        trans_lbl.set_xalign(0)
        row_trans.pack_start(trans_lbl, False, False, 0)

        transport_combo = Gtk.ComboBoxText.new()
        transport_combo.append("stdio", "stdio（本地命令行子进程）")
        transport_combo.append("http", "http（远程 Streamable HTTP 服务）")
        transport_combo.set_active_id(cfg.transport)
        transport_combo.set_hexpand(True)
        row_trans.pack_start(transport_combo, True, True, 0)

        header_vbox.pack_start(row_trans, False, False, 0)
        detail_vbox.pack_start(header_frame, False, False, 0)

        # ── 区域 2: 传输参数卡片 (Stdio vs HTTP) ──
        param_frame = Gtk.Frame.new()
        param_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        param_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        param_vbox.set_margin_start(10)
        param_vbox.set_margin_end(10)
        param_vbox.set_margin_top(8)
        param_vbox.set_margin_bottom(8)
        param_frame.add(param_vbox)

        # Stdio 容器
        stdio_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)

        row2_stdio = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        cmd_entry = Gtk.Entry.new()
        cmd_entry.set_placeholder_text("执行命令（如 npx 或 python3）")
        cmd_entry.set_text(cfg.command)
        cmd_entry.set_size_request(180, -1)
        row2_stdio.pack_start(cmd_entry, False, False, 0)

        args_entry = Gtk.Entry.new()
        args_entry.set_placeholder_text("命令参数（空格分隔，如 -y @modelcontextprotocol/server-filesystem /tmp）")
        args_entry.set_hexpand(True)
        args_entry.set_text(" ".join(cfg.args))
        row2_stdio.pack_start(args_entry, True, True, 0)
        stdio_box.pack_start(row2_stdio, False, False, 0)

        row2_5_stdio = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        cwd_entry = Gtk.Entry.new()
        cwd_entry.set_placeholder_text("工作目录 cwd（可选，如 /home/user/project）")
        cwd_entry.set_text(cfg.cwd or "")
        cwd_entry.set_hexpand(True)
        row2_5_stdio.pack_start(cwd_entry, True, True, 0)

        env_entry = Gtk.Entry.new()
        env_entry.set_placeholder_text("环境变量（可选，KEY=VAL, KEY2=VAL2）")
        env_entry.set_hexpand(True)
        env_text = ", ".join(f"{k}={v}" for k, v in (cfg.env or {}).items())
        env_entry.set_text(env_text)
        row2_5_stdio.pack_start(env_entry, True, True, 0)
        stdio_box.pack_start(row2_5_stdio, False, False, 0)

        param_vbox.pack_start(stdio_box, False, False, 0)

        # HTTP 容器
        http_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)

        row3_http = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        url_entry = Gtk.Entry.new()
        url_entry.set_placeholder_text("URL 端点（如 https://api.githubcopilot.com/mcp/）")
        url_entry.set_hexpand(True)
        url_entry.set_text(cfg.url)
        row3_http.pack_start(url_entry, True, True, 0)

        auth_combo = Gtk.ComboBoxText.new()
        auth_combo.append("none", "无认证")
        auth_combo.append("bearer", "Bearer Token")
        auth_combo.append("oauth2", "OAuth 2.1")
        auth_combo.set_active_id(cfg.auth_type or "bearer")
        auth_combo.set_tooltip_text("HTTP 认证方式")
        row3_http.pack_start(auth_combo, False, False, 0)

        api_key_entry = Gtk.Entry.new()
        api_key_entry.set_placeholder_text("API Key / Bearer Token")
        api_key_entry.set_text(cfg.api_key)
        api_key_entry.set_width_chars(25)
        api_key_entry.set_visibility(False)
        row3_http.pack_start(api_key_entry, False, False, 0)
        http_box.pack_start(row3_http, False, False, 0)

        row4_http_advanced = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        proto_combo = Gtk.ComboBoxText.new()
        proto_combo.append("2025-11-25", "2025-11-25（稳定标准）")
        proto_combo.append("2026-07-28", "2026-07-28（无状态新版）")
        proto_combo.set_active_id(cfg.protocol_version or "2025-11-25")
        proto_combo.set_tooltip_text("MCP 协议版本")
        row4_http_advanced.pack_start(proto_combo, False, False, 0)

        header_check = Gtk.CheckButton.new_with_label("发送 2026 新 Headers")
        header_check.set_active(cfg.enable_2026_headers)
        header_check.set_tooltip_text("启用 Mcp-Method / Mcp-Name 等 2026-07-28 请求头")
        row4_http_advanced.pack_start(header_check, False, False, 0)
        http_box.pack_start(row4_http_advanced, False, False, 0)

        # OAuth 2.1 配置行
        row5_oauth = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        oauth_client_id_entry = Gtk.Entry.new()
        oauth_client_id_entry.set_placeholder_text("Client ID（可选）")
        oauth_client_id_entry.set_text(cfg.oauth_client_id)
        oauth_client_id_entry.set_width_chars(14)
        row5_oauth.pack_start(oauth_client_id_entry, False, False, 0)

        oauth_client_secret_entry = Gtk.Entry.new()
        oauth_client_secret_entry.set_placeholder_text("Client Secret（可选）")
        oauth_client_secret_entry.set_text(cfg.oauth_client_secret)
        oauth_client_secret_entry.set_width_chars(14)
        oauth_client_secret_entry.set_visibility(False)
        row5_oauth.pack_start(oauth_client_secret_entry, False, False, 0)

        oauth_token_url_entry = Gtk.Entry.new()
        oauth_token_url_entry.set_placeholder_text("Token URL（可选）")
        oauth_token_url_entry.set_text(cfg.oauth_token_url)
        oauth_token_url_entry.set_width_chars(18)
        row5_oauth.pack_start(oauth_token_url_entry, False, False, 0)

        oauth_scopes_entry = Gtk.Entry.new()
        oauth_scopes_entry.set_placeholder_text("Scopes（可选）")
        oauth_scopes_entry.set_text(cfg.oauth_scopes)
        oauth_scopes_entry.set_width_chars(14)
        row5_oauth.pack_start(oauth_scopes_entry, False, False, 0)

        oauth_auth_btn = Gtk.Button.new_with_label("开始授权")
        oauth_auth_btn.set_tooltip_text("在浏览器中完成 OAuth 授权（PKCE）")
        row5_oauth.pack_start(oauth_auth_btn, False, False, 0)

        oauth_clear_btn = Gtk.Button.new_with_label("清除授权")
        oauth_clear_btn.set_tooltip_text("删除本地保存的 OAuth Token")
        row5_oauth.pack_start(oauth_clear_btn, False, False, 0)

        oauth_status_label = Gtk.Label.new()
        oauth_status_label.set_markup("<span foreground='#888'>未授权</span>")
        row5_oauth.pack_start(oauth_status_label, False, False, 0)
        http_box.pack_start(row5_oauth, False, False, 0)

        param_vbox.pack_start(http_box, False, False, 0)
        detail_vbox.pack_start(param_frame, False, False, 0)

        # ── 区域 3: 连接测试与状态卡片 ──
        test_frame = Gtk.Frame.new()
        test_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        test_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 10)
        test_box.set_margin_start(10)
        test_box.set_margin_end(10)
        test_box.set_margin_top(8)
        test_box.set_margin_bottom(8)
        test_frame.add(test_box)

        test_btn = Gtk.Button.new_with_label("⚡ 测试连接")
        test_btn.set_tooltip_text("发送 initialize 请求验证连通性与获取工具列表")
        test_box.pack_start(test_btn, False, False, 0)

        status_label = Gtk.Label.new()
        last_status = data.get("last_status", "") if data else ""
        if last_status == "connected":
            status_label.set_markup("<span foreground='green'>● 已连接</span>")
        elif last_status and last_status.startswith("error"):
            status_label.set_markup(f"<span foreground='red'>● {last_status}</span>")
        else:
            status_label.set_markup("<span foreground='#888'>○ 未连接</span>")
        test_box.pack_start(status_label, False, False, 0)
        detail_vbox.pack_start(test_frame, False, False, 0)

        # ── 3. 注册到 Stack 与组件引用列表 ──
        # 先对表单整体执行 show_all() 递归标记所有叶子控件为 visible
        detail_scrolled.show_all()

        # 再对动态互斥的子容器设置 no_show_all，防止顶层 dialog.show_all() 强制将它们同时展开
        stdio_box.set_no_show_all(True)
        http_box.set_no_show_all(True)
        api_key_entry.set_no_show_all(True)
        row5_oauth.set_no_show_all(True)

        self._mcp_stack.add_named(detail_scrolled, server_id)

        widget_item = {
            "server_id": server_id,
            "row": row,
            "page": detail_scrolled,
            "name": name_entry,
            "transport": transport_combo,
            "command": cmd_entry,
            "args": args_entry,
            "cwd": cwd_entry,
            "env": env_entry,
            "url": url_entry,
            "api_key": api_key_entry,
            "auth_type": auth_combo,
            "oauth_client_id": oauth_client_id_entry,
            "oauth_client_secret": oauth_client_secret_entry,
            "oauth_token_url": oauth_token_url_entry,
            "oauth_scopes": oauth_scopes_entry,
            "oauth_auth_btn": oauth_auth_btn,
            "oauth_clear_btn": oauth_clear_btn,
            "oauth_status_label": oauth_status_label,
            "protocol_version": proto_combo,
            "enable_2026_headers": header_check,
            "enabled": enabled_check,
            "auto_connect": auto_check,
            "status_label": status_label,
            "box": detail_scrolled,
        }
        self._mcp_server_widgets.append(widget_item)

        # ── 4. 显隐控制 ──
        def _apply_visibility(is_http_mode: bool):
            stdio_box.set_visible(not is_http_mode)
            http_box.set_visible(is_http_mode)
            mode = auth_combo.get_active_id()
            api_key_entry.set_visible(is_http_mode and mode == "bearer")
            row5_oauth.set_visible(is_http_mode and mode == "oauth2")

        is_http = (cfg.transport == "http")
        _apply_visibility(is_http)

        detail_scrolled.connect("map", lambda *_: _apply_visibility(
            transport_combo.get_active_id() == "http"
        ))

        # ── 5. 交互事件与双向联动 ──
        name_entry.connect("changed", lambda e: self._update_mcp_list_row(
            row, e.get_text().strip(), enabled_check.get_active(), transport_combo.get_active_id()
        ))
        enabled_check.connect("toggled", lambda s: (
            self._update_mcp_list_row(row, name_entry.get_text().strip(), s.get_active(), transport_combo.get_active_id()),
            self._update_mcp_count_label(),
        ))
        transport_combo.connect("changed", lambda cb: (
            _apply_visibility(cb.get_active_id() == "http"),
            self._update_mcp_list_row(row, name_entry.get_text().strip(), enabled_check.get_active(), cb.get_active_id()),
        ))

        # OAuth 状态与事件
        def _update_oauth_status():
            try:
                from mcp_integration.oauth.discovery import canonical_server_uri
                from mcp_integration.oauth.token_store import OAuthTokenStore
                url = url_entry.get_text().strip()
                if not url:
                    oauth_status_label.set_markup("<span foreground='#888'>未授权</span>")
                    return
                status = OAuthTokenStore(canonical_server_uri(url)).get_status()
                if status == "已授权":
                    oauth_status_label.set_markup("<span foreground='green'>● 已授权</span>")
                elif status == "已过期":
                    oauth_status_label.set_markup("<span foreground='orange'>● 已过期</span>")
                else:
                    oauth_status_label.set_markup("<span foreground='#888'>未授权</span>")
            except Exception:
                oauth_status_label.set_markup("<span foreground='#888'>未授权</span>")

        def _on_auth_changed(combo):
            is_http_mode = (transport_combo.get_active_id() == "http")
            mode = combo.get_active_id()
            api_key_entry.set_visible(is_http_mode and mode == "bearer")
            row5_oauth.set_visible(is_http_mode and mode == "oauth2")
            if is_http_mode and mode == "oauth2":
                _update_oauth_status()

        auth_combo.connect("changed", _on_auth_changed)
        _on_auth_changed(auth_combo)

        # 开始授权
        def _on_start_oauth_clicked(_btn):
            name = name_entry.get_text().strip()
            url = url_entry.get_text().strip()
            if not name or not url:
                oauth_status_label.set_markup("<span foreground='red'>● 需先填写名称和 URL</span>")
                return
            oauth_status_label.set_markup("<span foreground='orange'>● 授权中…请在浏览器完成</span>")

            def _do_auth():
                import asyncio
                try:
                    from mcp_integration.oauth.provider import OAuth2AuthProvider
                    provider = OAuth2AuthProvider(
                        url,
                        client_id=oauth_client_id_entry.get_text().strip(),
                        client_secret=oauth_client_secret_entry.get_text().strip(),
                        token_url=oauth_token_url_entry.get_text().strip(),
                        scopes=oauth_scopes_entry.get_text().strip(),
                    )
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(provider.get_auth_headers())
                    finally:
                        loop.close()
                    GLib.idle_add(lambda: oauth_status_label.set_markup(
                        "<span foreground='green'>● 已授权 ✓</span>"
                    ))
                except Exception as e:
                    err_msg = str(e)
                    GLib.idle_add(lambda msg=err_msg: oauth_status_label.set_markup(
                        f"<span foreground='red'>● 授权失败: {msg}</span>"
                    ))

            threading.Thread(target=_do_auth, daemon=True).start()

        oauth_auth_btn.connect("clicked", _on_start_oauth_clicked)

        # 清除授权
        def _on_clear_oauth_clicked(_btn):
            url = url_entry.get_text().strip()
            if not url:
                oauth_status_label.set_markup("<span foreground='#888'>未授权</span>")
                return
            try:
                from mcp_integration.oauth.discovery import canonical_server_uri
                from mcp_integration.oauth.token_store import OAuthTokenStore
                OAuthTokenStore(canonical_server_uri(url)).clear()
                oauth_status_label.set_markup("<span foreground='#888'>已清除授权</span>")
            except Exception as e:
                oauth_status_label.set_markup(f"<span foreground='red'>● 清除失败: {e}</span>")

        oauth_clear_btn.connect("clicked", _on_clear_oauth_clicked)

        # 测试连接
        test_btn.connect("clicked", lambda _: self._on_test_mcp_connection(
            name_entry, transport_combo,
            cmd_entry, args_entry,
            url_entry, api_key_entry, auth_combo,
            status_label,
        ))

        # 选中新添加项
        if select_new:
            self._mcp_list_box.select_row(row)
            self._mcp_stack.set_visible_child_name(server_id)
            name_entry.grab_focus()

        self._update_mcp_count_label()

    def _on_test_mcp_connection(
        self,
        name_entry,
        transport_combo,
        cmd_entry,
        args_entry,
        url_entry,
        api_key_entry,
        auth_combo,
        status_label,
    ):
        """测试 MCP Server 连接。启动真实的 initialize 握手验证连通性。"""
        name = name_entry.get_text().strip()
        transport = transport_combo.get_active_id()
        cmd = cmd_entry.get_text().strip()
        args_text = args_entry.get_text().strip()
        args_list = args_text.split() if args_text else []
        url = url_entry.get_text().strip()
        api_key = api_key_entry.get_text().strip()
        auth_type = auth_combo.get_active_id()

        # 读取卡片配置（通过卡片 widgets 中保存的 entry）
        card_cfg = next(
            (w for w in self._mcp_server_widgets if w["name"] is name_entry),
            None,
        )
        cwd_val = None
        env_dict = {}
        oauth_client_id = ""
        oauth_client_secret = ""
        oauth_token_url = ""
        oauth_scopes = ""
        if card_cfg is not None:
            if card_cfg.get("cwd"):
                cwd_val = card_cfg["cwd"].get_text().strip() or None
            if card_cfg.get("env"):
                env_dict = self._parse_env_str(card_cfg["env"].get_text().strip())
            oauth_client_id = card_cfg["oauth_client_id"].get_text().strip() if card_cfg.get("oauth_client_id") else ""
            oauth_client_secret = card_cfg["oauth_client_secret"].get_text().strip() if card_cfg.get("oauth_client_secret") else ""
            oauth_token_url = card_cfg["oauth_token_url"].get_text().strip() if card_cfg.get("oauth_token_url") else ""
            oauth_scopes = card_cfg["oauth_scopes"].get_text().strip() if card_cfg.get("oauth_scopes") else ""

        if not name:
            status_label.set_markup("<span foreground='red'>● 名称不能为空</span>")
            return

        if transport == "http":
            if not url:
                status_label.set_markup("<span foreground='red'>● URL 不能为空</span>")
                return
        else:  # stdio
            if not cmd:
                status_label.set_markup("<span foreground='red'>● 命令不能为空</span>")
                return

        status_label.set_markup("<span foreground='orange'>● 测试中…</span>")

        def _do_test():
            import asyncio
            try:
                config = MCPServerConfig(
                    name=name,
                    transport=transport,
                    command=cmd,
                    args=args_list,
                    cwd=cwd_val,
                    env=env_dict,
                    url=url,
                    api_key=api_key,
                    auth_type=auth_type,
                    oauth_client_id=oauth_client_id,
                    oauth_client_secret=oauth_client_secret,
                    oauth_token_url=oauth_token_url,
                    oauth_scopes=oauth_scopes,
                    auto_connect=False,
                )

                mgr = MCPClientManager()
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    ok, msg = loop.run_until_complete(mgr.connect_by_config(config))
                    if ok:
                        loop.run_until_complete(mgr.disconnect(name))
                        GLib.idle_add(
                            lambda: status_label.set_markup(
                                "<span foreground='green'>● 测试通过 ✓</span>"
                            )
                        )
                    else:
                        GLib.idle_add(
                            lambda: status_label.set_markup(
                                f"<span foreground='red'>● {msg}</span>"
                            )
                        )
                finally:
                    loop.close()
            except Exception as e:
                err_msg = str(e)
                GLib.idle_add(
                    lambda msg=err_msg: status_label.set_markup(
                        f"<span foreground='red'>● 测试异常: {msg}</span>"
                    )
                )

        threading.Thread(target=_do_test, daemon=True).start()

    def _remove_mcp_server_card(self, target):
        """移除一个 MCP 服务器配置（支持传入 server_id 或 widget box/frame）。"""
        item = None
        if isinstance(target, str):
            item = next((w for w in self._mcp_server_widgets if w.get("server_id") == target), None)
        else:
            item = next((w for w in self._mcp_server_widgets if w.get("box") is target or w.get("page") is target), None)

        if not item:
            return

        # 焦点安全防护：移除控件前先清除焦点，防止底层 C 对象销毁悬空
        if self._dialog:
            self._dialog.set_focus(None)

        server_id = item.get("server_id")
        row = item.get("row")
        page = item.get("page")

        # 确定删除后应选中的下一个行
        next_select_row = None
        if row:
            idx = row.get_index()
            children = self._mcp_list_box.get_children()
            if len(children) > 1:
                if idx + 1 < len(children):
                    next_select_row = children[idx + 1]
                elif idx - 1 >= 0:
                    next_select_row = children[idx - 1]

        self._mcp_server_widgets = [w for w in self._mcp_server_widgets if w.get("server_id") != server_id]

        if row and row.get_parent() == self._mcp_list_box:
            self._mcp_list_box.remove(row)
        if page and page.get_parent() == self._mcp_stack:
            self._mcp_stack.remove(page)

        self._update_mcp_count_label()

        if next_select_row:
            self._mcp_list_box.select_row(next_select_row)
        else:
            first = self._mcp_list_box.get_row_at_index(0)
            if first:
                self._mcp_list_box.select_row(first)
            else:
                self._mcp_stack.set_visible_child_name("empty")
