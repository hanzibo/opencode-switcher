#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────
# OpenCode Switcher - 一键安装脚本
# ──────────────────────────────────────────────

INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/opencode-switcher}"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
SYSD_DIR="$HOME/.config/systemd/user"

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
    for cmd in python3 pip3 xdotool; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done

    if ! command -v gnome-terminal &>/dev/null; then
        warn "gnome-terminal 未安装，需要安装: sudo apt install gnome-terminal"
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

    # Optional: opencode CLI
    if ! command -v opencode &>/dev/null; then
        warn "opencode CLI 未找到，需另行安装（npm install -g @suyash-thakur/opencode）"
    fi
}

# ── Install system deps ───────────────────────
install_system_deps() {
    info "安装系统依赖..."
    sudo apt update -qq
    sudo apt install -y -qq \
        xdotool \
        gnome-terminal \
        gir1.2-ayatanaappindicator3-0.1 \
        python3-gi \
        python3-pip \
        2>&1 | tail -1
    info "系统依赖安装完成"
}

# ── Install Python deps ───────────────────────
install_python_deps() {
    info "安装 Python 依赖..."
    pip3 install --user --quiet \
        "PyGObject>=3.42" \
        "pynput>=1.7" \
        "python-xlib>=0.33"
    info "Python 依赖安装完成"
}

# ── Install files ─────────────────────────────
install_files() {
    info "安装文件到: $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$APP_DIR" "$SYSD_DIR"

    # Copy source files
    cp "$SCRIPT_DIR/main.py"           "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/panel.py"          "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/hotkey.py"         "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/launcher.py"       "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/session_store.py"  "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/run.sh"            "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/opencode-switcher.png" "$INSTALL_DIR/"
    chmod +x "$INSTALL_DIR/run.sh"

    # Generate .desktop file with correct paths
    sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        "$SCRIPT_DIR/opencode-switcher.desktop" \
        > "$APP_DIR/opencode-switcher.desktop"
    chmod 644 "$APP_DIR/opencode-switcher.desktop"

    # Generate systemd service with correct paths
    sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        "$SCRIPT_DIR/opencode-switcher.service" \
        > "$SYSD_DIR/opencode-switcher.service"

    # Create wrapper script in PATH
    cat > "$BIN_DIR/opencode-switcher" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/run.sh"
EOF
    chmod +x "$BIN_DIR/opencode-switcher"

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
    echo "  快捷键: Ctrl+Shift+Space 呼出面板"
    echo "  手动启动: opencode-switcher"
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
    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
        info "删除: $INSTALL_DIR"
    fi

    # Remove lock file
    rm -f "$HOME/.config/opencode-switcher/lock"

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
        echo -e "  opencode: ${YELLOW}未安装 (npm install -g @suyash-thakur/opencode)${NC}"
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
