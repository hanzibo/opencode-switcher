#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────
# OpenCode Switcher - 一键安装脚本
# ──────────────────────────────────────────────

INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/opencode-switcher}"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
SYSD_DIR="$HOME/.config/systemd/user"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/clipboard-monitor@opencode-switcher"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
VERSION="1.0.0"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERR]${NC} $*"; }

# ── Help ──────────────────────────────────────
usage() {
    cat <<EOF
OpenCode Switcher v${VERSION} - 安装/卸载/状态检查

用法: $0 [command]

命令:
  install    安装到 \${INSTALL_DIR:-~/.local/share/opencode-switcher}（默认）
  uninstall  卸载
  status     检查安装状态
  help       显示本帮助

环境变量:
  INSTALL_DIR  自定义安装目录（默认: ~/.local/share/opencode-switcher）
EOF
}

# ── Dependency checks ─────────────────────────
check_deps() {
    local missing=()
    if ! command -v python3 &>/dev/null; then
        missing+=("python3")
    fi

    if ! command -v ptyxis &>/dev/null && ! command -v gnome-terminal &>/dev/null; then
        warn "未找到 ptyxis 或 gnome-terminal，请安装其中之一"
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        error "缺少系统依赖: ${missing[*]}"
        echo "请安装: sudo apt install ${missing[*]} gir1.2-ayatanaappindicator3-0.1"
        exit 1
    fi

    # Check Python packages
    local py_missing=()
    for mod in gi pynput; do
        if ! python3 -c "import $mod" 2>/dev/null; then
            py_missing+=("$mod")
        fi
    done

    if [ ${#py_missing[@]} -gt 0 ]; then
        warn "缺少 Python 包: ${py_missing[*]}"
        echo "将通过 pip 自动安装"
    fi

    # Optional: wl-clipboard (Wayland clipboard)
    if ! command -v wl-paste &>/dev/null; then
        warn "wl-clipboard 未安装（可选，Wayland 下剪切板监听/写入需要）: sudo apt install wl-clipboard"
    fi

    # Optional: xclip (X11 clipboard)
    if ! command -v xclip &>/dev/null; then
        warn "xclip 未安装（可选，X11 下剪切板监听/写入需要）: sudo apt install xclip"
    fi

    # Optional: opencode CLI
    if ! command -v opencode &>/dev/null; then
        warn "opencode CLI 未找到，需另行安装（npm install -g opencode-ai）"
    fi
}

# ── Install system deps ───────────────────────
install_system_deps() {
    local missing_sys=()
    if ! command -v dpkg &>/dev/null; then
        info "未检测到 dpkg，正在尝试直接执行安装程序..."
        sudo apt update -qq && sudo apt install -y -qq gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pip python3-venv wl-clipboard 2>&1 | tail -1
        return
    fi

    for pkg in gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pip python3-venv wl-clipboard; do
        if ! dpkg -s "$pkg" &>/dev/null; then
            missing_sys+=("$pkg")
        fi
    done

    if [ ${#missing_sys[@]} -gt 0 ]; then
        info "安装系统依赖: ${missing_sys[*]}..."
        sudo apt update -qq
        sudo apt install -y -qq "${missing_sys[@]}" 2>&1 | tail -1
        info "系统依赖安装完成"
    else
        info "系统依赖已满足，无需安装。"
    fi

    if ! python3 -m pip --version &>/dev/null; then
        error "pip 安装失败，请手动安装 python3-pip 后重试"
        exit 1
    fi
    if ! command -v xdotool &>/dev/null; then
        warn "xdotool 未安装（可选，仅 X11 下窗口激活需要）"
    fi
    if ! command -v ptyxis &>/dev/null && ! command -v gnome-terminal &>/dev/null; then
        warn "未检测到 ptyxis 或 gnome-terminal，请安装一个: sudo apt install ptyxis"
    fi
}

# ── Install Python deps ───────────────────────
install_python_deps() {
    info "安装 Python 依赖..."
    mkdir -p "$INSTALL_DIR"
    python3 -m venv --system-site-packages "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install --quiet \
        "pynput>=1.7" \
        "python-xlib>=0.33"
    info "Python 依赖安装完成"
}

# ── Install files ─────────────────────────────
install_files() {
    info "安装文件到: $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$APP_DIR" "$SYSD_DIR"

    # Copy source files
    cp "$SCRIPT_DIR/main.py"                     "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/panel.py"                    "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/hotkey.py"                   "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/launcher.py"                 "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/session_store.py"            "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/clipboard_store.py"          "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/clipboard_panel.py"          "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/utils.py"                    "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/run.sh"                      "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/opencode-switcher-toggle"    "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/opencode-switcher.png"       "$INSTALL_DIR/"
    chmod +x "$INSTALL_DIR/run.sh"
    chmod +x "$INSTALL_DIR/opencode-switcher-toggle"

    # Generate .desktop file with correct paths
    sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        "$SCRIPT_DIR/opencode-switcher.desktop" \
        > "$APP_DIR/opencode-switcher.desktop"
    chmod 644 "$APP_DIR/opencode-switcher.desktop"

    # Generate systemd service with correct paths
    sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        "$SCRIPT_DIR/opencode-switcher.service" \
        > "$SYSD_DIR/opencode-switcher.service"

    # Create wrapper scripts in PATH
    cat > "$BIN_DIR/opencode-switcher" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/run.sh"
EOF
    chmod +x "$BIN_DIR/opencode-switcher"

    cat > "$BIN_DIR/opencode-switcher-toggle" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/opencode-switcher-toggle"
EOF
    chmod +x "$BIN_DIR/opencode-switcher-toggle"

    # Install GNOME Shell extension
    if [ -d "$SCRIPT_DIR/gnome-extension" ]; then
        mkdir -p "$EXT_DIR"
        cp "$SCRIPT_DIR/gnome-extension/extension.js" "$EXT_DIR/"
        cp "$SCRIPT_DIR/gnome-extension/metadata.json" "$EXT_DIR/"
        info "GNOME Shell 扩展已安装到: $EXT_DIR"
        if command -v gnome-extensions &>/dev/null; then
            gnome-extensions enable clipboard-monitor@opencode-switcher 2>/dev/null && \
                info "GNOME Shell 扩展已启用" || \
                warn "扩展已安装，请登出再登入后手动启用: gnome-extensions enable clipboard-monitor@opencode-switcher"
        fi
    fi

    info "文件安装完成"
}

# ── Enable systemd service ────────────────────
enable_service() {
    info "启用 systemd 用户服务..."
    systemctl --user daemon-reload
    systemctl --user enable --now opencode-switcher.service
    info "服务已启动 (systemctl --user status opencode-switcher)"
}

# ── Install command ───────────────────────────
cmd_install() {
    echo "=========================================="
    echo " OpenCode Switcher v${VERSION} 安装"
    echo "=========================================="
    echo ""

    check_deps
    echo ""
    install_system_deps
    echo ""
    install_python_deps
    echo ""
    install_files
    echo ""
    enable_service

    echo ""
    info "安装完成!"
    echo "  安装目录: $INSTALL_DIR"
    echo "  桌面入口: $APP_DIR/opencode-switcher.desktop"
    echo "  系统服务: opencode-switcher.service"
    echo ""
    echo "  ┌─ 下一步手动配置 ──────────────────────────┐"
    echo "  │                                          │"
    echo "  │  1. 快捷键: GNOME 设置 → 键盘 → 自定义    │"
    echo "  │     名称: OpenCode Switcher               │"
    echo "  │     命令: opencode-switcher-toggle         │"
    echo "  │     绑定: Ctrl+Shift+Space                 │"
    echo "  │                                          │"
    echo "  │  2. 如果扩展未启用 (见上一步提示):          │"
    echo "  │     gnome-extensions enable               │"
    echo "  │       clipboard-monitor@opencode-switcher  │"
    echo "  │     然后登出再登入                         │"
    echo "  │                                          │"
    echo "  │  3. 确保 opencode CLI 可用:                │"
    echo "  │     npm install -g opencode-ai │"
    echo "  │                                          │"
    echo "  └──────────────────────────────────────────┘"
    echo ""
    echo "  手动启动: opencode-switcher"
    echo "  系统服务: systemctl --user status opencode-switcher"
    echo ""
}

# ── Uninstall ─────────────────────────────────
cmd_uninstall() {
    echo "=========================================="
    echo " 卸载 OpenCode Switcher"
    echo "=========================================="
    echo ""

    # Stop and disable service
    if systemctl --user is-active --quiet opencode-switcher.service 2>/dev/null; then
        info "停止服务..."
        systemctl --user stop opencode-switcher.service
    fi
    if systemctl --user is-enabled --quiet opencode-switcher.service 2>/dev/null; then
        info "禁用服务..."
        systemctl --user disable opencode-switcher.service
    fi
    systemctl --user daemon-reload

    # Remove files
    rm -f "$APP_DIR/opencode-switcher.desktop"
    rm -f "$SYSD_DIR/opencode-switcher.service"
    rm -f "$BIN_DIR/opencode-switcher"
    rm -f "$BIN_DIR/opencode-switcher-toggle"
    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
        info "删除: $INSTALL_DIR"
    fi
    if [ -d "$EXT_DIR" ]; then
        if command -v gnome-extensions &>/dev/null; then
            gnome-extensions disable clipboard-monitor@opencode-switcher 2>/dev/null && \
                info "GNOME Shell 扩展已禁用"
        fi
        rm -rf "$EXT_DIR"
        info "删除: $EXT_DIR"
    fi

    # Remove lock file
    rm -f "$HOME/.config/opencode-switcher/lock"

    # Remove config and cache directories (user data)
    if [ -d "$HOME/.config/opencode-switcher" ] || [ -d "$HOME/.cache/opencode-switcher" ]; then
        echo ""
        warn "是否保留剪切板历史等用户数据？"
        echo -n "  输入 y 保留, n 删除 [y]: "
        read -r keep_data
        if [ "$keep_data" = "n" ] || [ "$keep_data" = "N" ]; then
            rm -rf "$HOME/.config/opencode-switcher" 2>/dev/null
            rm -rf "$HOME/.cache/opencode-switcher" 2>/dev/null
            info "用户数据已删除"
        else
            info "用户数据已保留"
        fi
    fi

    info "卸载完成"
}

# ── Status ────────────────────────────────────
cmd_status() {
    echo "OpenCode Switcher 状态"
    echo "====================="

    # Check install dir
    if [ -f "$INSTALL_DIR/main.py" ]; then
        echo -e "  安装目录: ${GREEN}已安装${NC} ($INSTALL_DIR)"
    else
        echo -e "  安装目录: ${RED}未安装${NC}"
    fi

    # Check desktop entry
    if [ -f "$APP_DIR/opencode-switcher.desktop" ]; then
        echo -e "  桌面入口: ${GREEN}已安装${NC}"
    else
        echo -e "  桌面入口: ${RED}未安装${NC}"
    fi

    # Check service
    if systemctl --user is-active --quiet opencode-switcher.service 2>/dev/null; then
        echo -e "  系统服务: ${GREEN}运行中${NC}"
    else
        echo -e "  系统服务: ${RED}未运行${NC}"
    fi

    # Check opencode CLI
    if command -v opencode &>/dev/null; then
        echo -e "  opencode: ${GREEN}$(opencode --version 2>/dev/null || echo '已安装')${NC}"
    else
        echo -e "  opencode: ${YELLOW}未安装 (npm install -g opencode-ai)${NC}"
    fi

    # Check GNOME Shell extension
    if [ -d "$EXT_DIR" ]; then
        if command -v gnome-extensions &>/dev/null && \
           gnome-extensions info clipboard-monitor@opencode-switcher &>/dev/null; then
            echo -e "  GNOME 扩展: ${GREEN}已安装${NC}"
        else
            echo -e "  GNOME 扩展: ${YELLOW}已安装，但未启用 (运行 gnome-extensions enable clipboard-monitor@opencode-switcher)${NC}"
        fi
    else
        echo -e "  GNOME 扩展: ${YELLOW}未安装${NC}"
    fi

    # Check toggle wrapper
    if [ -f "$BIN_DIR/opencode-switcher-toggle" ]; then
        echo -e "  触发脚本: ${GREEN}已安装${NC}"
    else
        echo -e "  触发脚本: ${RED}未安装${NC}"
    fi
}

# ── Main ──────────────────────────────────────
case "${1:-install}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    status)    cmd_status ;;
    help|--help|-h) usage ;;
    *)
        error "未知命令: $1"
        usage
        exit 1
        ;;
esac
