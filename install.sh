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

# ── 共享系统配置 ──────────────────────────────
# 系统运行依赖（Debian/Ubuntu 包名）— 单一事实来源：
# 安装、检测、status 报告均基于此数组。
# 注意：WebKit2GTK 不在强制列表中——4.1/4.0 由 resolve_webkit_package 动态解析，
# 4.0-only 系统不应被强制安装 4.1。
SYS_PACKAGES=(
    gir1.2-ayatanaappindicator3-0.1
    python3-gi
    python3-gi-cairo
    python3-pip
    python3-venv
    wl-clipboard
)

# WebKit2GTK 绑定包：4.1 优先，仅提供 4.0 的发行版回退到 4.0
WEBKIT_PACKAGES=(gir1.2-webkit2-4.1 gir1.2-webkit2-4.0)

# 支持的终端（必须与 system/launcher.py 的 _TERMINALS 保持一致）
TERMINALS=(ptyxis gnome-terminal kgx blackbox)

# 检测已安装的第一个受支持终端
detect_terminal() {
    for term in "${TERMINALS[@]}"; do
        if command -v "$term" &>/dev/null; then
            echo "$term"
            return 0
        fi
    done
    return 1
}

# 检查 WebKit2 运行时绑定（与应用使用相同的 4.1 → 4.0 回退逻辑）
check_webkit2() {
    if python3 -c "import gi; gi.require_version('WebKit2', '4.1'); from gi.repository import WebKit2" 2>/dev/null; then
        echo "4.1"
        return 0
    fi
    if python3 -c "import gi; gi.require_version('WebKit2', '4.0'); from gi.repository import WebKit2" 2>/dev/null; then
        echo "4.0"
        return 0
    fi
    warn "WebKit2 运行时绑定缺失，AI 助手面板将无法正常使用"
    warn "请安装: sudo apt install gir1.2-webkit2-4.1（4.0-only 系统请改用 gir1.2-webkit2-4.0）"
    return 1
}

# 解析可用的 WebKit2 包：优先 4.1，回退 4.0；任一版本已安装时输出空串
# （安装、dpkg 探测与无 dpkg 回退路径均以此为准，避免 4.0-only 系统被强制装 4.1）
resolve_webkit_package() {
    local pkg
    for pkg in "${WEBKIT_PACKAGES[@]}"; do
        if command -v dpkg &>/dev/null && dpkg -s "$pkg" &>/dev/null; then
            echo ""
            return 0
        fi
    done
    if command -v apt-cache &>/dev/null; then
        for pkg in "${WEBKIT_PACKAGES[@]}"; do
            if apt-cache policy "$pkg" 2>/dev/null | grep -q "Candidate: [0-9]"; then
                echo "$pkg"
                return 0
            fi
        done
    fi
    echo "gir1.2-webkit2-4.1"
    return 0
}

# 安全检查 Python 模块可导入性：模块名经 argv 传给 importlib，
# 绝不拼入 python -c 源码（防止 requirements.txt 内容注入）
py_import_ok() {
    local pybin="$1"
    local mod="$2"
    case "$mod" in
        [A-Za-z_][A-Za-z0-9_.]*)
            "$pybin" -c "import importlib, sys; importlib.import_module(sys.argv[1])" "$mod" 2>/dev/null
            ;;
        *)
            return 1
            ;;
    esac
}

# 校验 INSTALL_DIR：展开开头的 ~、去除尾部斜杠；拒绝空值、相对路径、
# 根目录 /、主目录 $HOME、含 . / .. 路径分量的值以及非法字符。
# 必须在任何 install/uninstall 使用之前调用（防止 rm -rf / pgrep 等误伤）。
validate_install_dir() {
    case "$INSTALL_DIR" in
        "~"|"~/"*)
            INSTALL_DIR="$HOME${INSTALL_DIR#\~}"
            ;;
    esac
    while [ "$INSTALL_DIR" != "/" ] && [ "${INSTALL_DIR%/}" != "$INSTALL_DIR" ]; do
        INSTALL_DIR="${INSTALL_DIR%/}"
    done
    if [ -z "$INSTALL_DIR" ]; then
        error "INSTALL_DIR 不能为空"
        exit 1
    fi
    # ~ 展开与去尾斜杠之后必须是绝对路径（相对路径会被误用于 rm -rf/pgrep）
    case "$INSTALL_DIR" in
        /*) ;;
        *)
            error "INSTALL_DIR 必须是绝对路径: $INSTALL_DIR"
            exit 1
            ;;
    esac
    if [ "$INSTALL_DIR" = "/" ]; then
        error "INSTALL_DIR 不能是根目录 /"
        exit 1
    fi
    if [ "$INSTALL_DIR" = "${HOME%/}" ]; then
        error "INSTALL_DIR 不能是主目录 ($HOME)"
        exit 1
    fi
    case "$INSTALL_DIR" in
        *"/../"*|*"/.."|".."|"../"*|*"/./"*|*"/."|"."|"./"*)
            error "INSTALL_DIR 不能包含 . 或 .. 路径分量: $INSTALL_DIR"
            exit 1
            ;;
        *[!A-Za-z0-9._/+_-]*)
            error "INSTALL_DIR 包含非法字符: $INSTALL_DIR"
            exit 1
            ;;
    esac
    return 0
}

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

    if ! detect_terminal &>/dev/null; then
        warn "未找到受支持的终端 (ptyxis / gnome-terminal / kgx / blackbox)，请安装其中之一: sudo apt install ptyxis"
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        error "缺少系统依赖: ${missing[*]}"
        echo "请安装: sudo apt install ${missing[*]} ${SYS_PACKAGES[*]}"
        exit 1
    fi

    # Check Python packages
    local py_missing=()
    for mod in gi cairo; do
        if ! py_import_ok python3 "$mod"; then
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



    # Optional: opencode CLI
    if ! command -v opencode &>/dev/null; then
        warn "opencode CLI 未找到，需另行安装（npm install -g opencode-ai）"
    fi
}

# ── Install system deps ───────────────────────
install_system_deps() {
    local missing_sys=()

    # 无 dpkg 回退路径：先刷新 apt 元数据，再解析 WebKit2 包。
    # resolve_webkit_package 依赖 apt-cache policy 的候选信息，元数据过期时
    # 4.1/4.0 候选会缺失，全新安装将无法正确解析版本。
    if ! command -v dpkg &>/dev/null; then
        info "未检测到 dpkg，正在尝试直接执行安装程序..."
        sudo apt update -qq
        local webkit_pkg
        webkit_pkg="$(resolve_webkit_package)"
        local pkg_list=("${SYS_PACKAGES[@]}")
        if [ -n "$webkit_pkg" ]; then
            pkg_list+=("$webkit_pkg")
        fi
        sudo apt install -y -qq "${pkg_list[@]}" 2>&1 | tail -1
        return
    fi

    # dpkg 路径同样先 apt update 再 resolve：apt-cache policy 必须基于新鲜
    # 元数据判断 4.1/4.0 可用性，否则全新安装可能解析到错误候选。
    sudo apt update -qq

    local webkit_pkg
    webkit_pkg="$(resolve_webkit_package)"
    local pkg_list=("${SYS_PACKAGES[@]}")
    if [ -n "$webkit_pkg" ]; then
        pkg_list+=("$webkit_pkg")
    fi

    for pkg in "${pkg_list[@]}"; do
        if ! dpkg -s "$pkg" &>/dev/null; then
            missing_sys+=("$pkg")
        fi
    done

    if [ ${#missing_sys[@]} -gt 0 ]; then
        info "安装系统依赖: ${missing_sys[*]}..."
        sudo apt install -y -qq "${missing_sys[@]}" 2>&1 | tail -1
        info "系统依赖安装完成"
    else
        info "系统依赖已满足，无需安装。"
    fi

    if ! python3 -m pip --version &>/dev/null; then
        error "pip 安装失败，请手动安装 python3-pip 后重试"
        exit 1
    fi

    # WebKit2 运行时绑定检查（AI 面板必需，缺失时打印 apt 提示）
    check_webkit2 || true

    if ! detect_terminal &>/dev/null; then
        warn "未检测到受支持的终端 (ptyxis / gnome-terminal / kgx / blackbox)，请安装一个: sudo apt install ptyxis"
    fi
}

# ── Install Python deps ───────────────────────
install_python_deps() {
    info "安装 Python 依赖..."
    mkdir -p "$INSTALL_DIR"
    python3 -m venv --system-site-packages "$INSTALL_DIR/venv"
    # 从 requirements.txt 安装（单一事实来源）
    "$INSTALL_DIR/venv/bin/pip" install --quiet \
        -r "$SCRIPT_DIR/requirements.txt"
    # tiktoken 可选安装（token 计数更精准，安装失败不影响核心功能）
    "$INSTALL_DIR/venv/bin/pip" install --quiet "tiktoken>=0.7" 2>/dev/null || true
    info "Python 依赖安装完成"
}

# ── Install files ─────────────────────────────
install_files() {
    info "安装文件到: $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$APP_DIR" "$SYSD_DIR"

    # 清理上一次安装的应用文件，避免 cp -r 重装后残留源中已删除的幽灵文件。
    # 只删除本脚本复制的文件/目录；venv 虚拟环境与用户数据一律保留。
    rm -rf "$INSTALL_DIR/views" \
        "$INSTALL_DIR/dialogs" \
        "$INSTALL_DIR/stores" \
        "$INSTALL_DIR/ai_engine" \
        "$INSTALL_DIR/system" \
        "$INSTALL_DIR/tool_registry" \
        "$INSTALL_DIR/ai_text_utils" \
        "$INSTALL_DIR/mcp_integration" \
        "$INSTALL_DIR/html_templates" \
        "$INSTALL_DIR/katex"
    rm -f "$INSTALL_DIR/main.py" \
        "$INSTALL_DIR/run.sh" \
        "$INSTALL_DIR/opencode-switcher-toggle" \
        "$INSTALL_DIR/opencode-switcher.png"

    # Copy source files
    cp "$SCRIPT_DIR/main.py"                     "$INSTALL_DIR/"
    # Copy package directories
    cp -r "$SCRIPT_DIR/views"                    "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/dialogs"                  "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/stores"                   "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/ai_engine"                "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/system"                   "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/tool_registry"            "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/ai_text_utils"            "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/mcp_integration"          "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/html_templates"           "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/run.sh"                      "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/opencode-switcher-toggle"    "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/deploy/opencode-switcher.png" "$INSTALL_DIR/"
    # Copy KaTeX resources (math rendering in AI panel)
    if [ -d "$SCRIPT_DIR/katex" ]; then
        cp -r "$SCRIPT_DIR/katex"                 "$INSTALL_DIR/"
        info "KaTeX 资源已复制 ($(find "$SCRIPT_DIR/katex" -type f | wc -l) 文件)"
    fi
    chmod +x "$INSTALL_DIR/run.sh"
    chmod +x "$INSTALL_DIR/opencode-switcher-toggle"

    # Generate .desktop file with correct paths
    sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        "$SCRIPT_DIR/deploy/opencode-switcher.desktop" \
        > "$APP_DIR/opencode-switcher.desktop"
    chmod 644 "$APP_DIR/opencode-switcher.desktop"

    # Generate systemd service with correct paths
    sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        "$SCRIPT_DIR/deploy/opencode-switcher.service" \
        > "$SYSD_DIR/opencode-switcher.service"

    # Create wrapper scripts in PATH
    cat > "$BIN_DIR/opencode-switcher" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/run.sh"
EOF
    chmod +x "$BIN_DIR/opencode-switcher"

    cat > "$BIN_DIR/opencode-switcher-toggle" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/opencode-switcher-toggle" "\$@"
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
    if ! command -v systemctl &>/dev/null || ! systemctl --user daemon-reload 2>/dev/null; then
        warn "未检测到可用的 systemd 用户会话，跳过服务注册（应用仍可手动启动）"
        warn "可稍后手动启用: systemctl --user enable --now opencode-switcher.service"
        return 0
    fi
    if systemctl --user enable --now opencode-switcher.service 2>/dev/null; then
        info "服务已启动 (systemctl --user status opencode-switcher)"
    else
        warn "服务注册失败，可稍后手动启用: systemctl --user enable --now opencode-switcher.service"
    fi
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
    echo "  │  2. AI快捷键: GNOME 设置 → 键盘 → 自定义  │"
    echo "  │     名称: OpenCode Switcher AI            │"
    echo "  │     命令: opencode-switcher-toggle --ai    │"
    echo "  │     绑定: Ctrl+Shift+X                     │"
    echo "  │                                          │"
    echo "  │  3. 如果扩展未启用 (见上一步提示):          │"
    echo "  │     gnome-extensions enable               │"
    echo "  │       clipboard-monitor@opencode-switcher  │"
    echo "  │     然后登出再登入                         │"
    echo "  │                                          │"
    echo "  │  4. 确保 opencode CLI 可用:                │"
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
        systemctl --user stop opencode-switcher.service 2>/dev/null || \
            warn "停止服务失败（systemd 用户会话不可用？），跳过"
    fi
    if systemctl --user is-enabled --quiet opencode-switcher.service 2>/dev/null; then
        info "禁用服务..."
        systemctl --user disable opencode-switcher.service 2>/dev/null || \
            warn "禁用服务失败（systemd 用户会话不可用？），跳过"
    fi
    # daemon-reload 失败（无 systemd 用户会话）不中止卸载，仅告警
    if ! command -v systemctl &>/dev/null || ! systemctl --user daemon-reload 2>/dev/null; then
        warn "未检测到可用的 systemd 用户会话，跳过 daemon-reload（不影响文件清理）"
    fi

    # 终止手动启动的进程（非 systemd 管理）。先粗筛候选 PID，再用固定字符串
    # 核对 /proc cmdline 是否包含精确的安装路径，避免 INSTALL_DIR 中的
    # 正则元字符（. + 等）被 pgrep -f 展开或误杀同名路径的进程。
    local running_pids=()
    while IFS='' read -r pid; do
        if [ -r "/proc/$pid/cmdline" ] && \
           tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -Fq -- "$INSTALL_DIR/main.py"; then
            running_pids+=("$pid")
        fi
    done < <(pgrep -f "python" 2>/dev/null || true)
    if [ ${#running_pids[@]} -gt 0 ]; then
        info "检测到手动启动的进程 (PID: ${running_pids[*]})，正在终止..."
        kill "${running_pids[@]}" 2>/dev/null || true
        sleep 1
        # 强制终止仍未退出的进程
        for pid in "${running_pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                warn "进程 $pid 未响应 SIGTERM，执行 SIGKILL..."
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
        info "手动进程已终止"
    fi

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

    # Check supported terminal
    if term=$(detect_terminal); then
        echo -e "  终端: ${GREEN}$term${NC} (ptyxis / gnome-terminal / kgx / blackbox)"
    else
        echo -e "  终端: ${YELLOW}未检测到受支持的终端${NC} (ptyxis / gnome-terminal / kgx / blackbox)"
    fi

    # Check system dependencies (shared package array)
    echo ""
    echo "  ── 系统依赖 ──"
    if command -v dpkg &>/dev/null; then
        for pkg in "${SYS_PACKAGES[@]}"; do
            if dpkg -s "$pkg" &>/dev/null; then
                echo -e "    ${GREEN}✔${NC} $pkg"
            else
                echo -e "    ${RED}✘${NC} $pkg (缺失)"
            fi
        done
        # WebKit2 包：4.1 优先，4.0 回退
        if dpkg -s gir1.2-webkit2-4.1 &>/dev/null; then
            echo -e "    ${GREEN}✔${NC} gir1.2-webkit2-4.1"
        elif dpkg -s gir1.2-webkit2-4.0 &>/dev/null; then
            echo -e "    ${GREEN}✔${NC} gir1.2-webkit2-4.0"
        else
            echo -e "    ${RED}✘${NC} WebKit2 包缺失 (gir1.2-webkit2-4.1 或 gir1.2-webkit2-4.0)"
        fi
    else
        echo -e "    ${YELLOW}无法检查（未检测到 dpkg）${NC}"
    fi

    # Runtime binding checks (gi / cairo / WebKit2)
    for mod in gi cairo; do
        if py_import_ok python3 "$mod"; then
            echo -e "    ${GREEN}✔${NC} python 模块: $mod"
        else
            echo -e "    ${RED}✘${NC} python 模块: $mod (缺失)"
        fi
    done
    if wk=$(check_webkit2 2>/dev/null); then
        echo -e "    ${GREEN}✔${NC} WebKit2 运行时绑定 (${wk})"
    else
        echo -e "    ${RED}✘${NC} WebKit2 运行时绑定 缺失 — 运行: sudo apt install gir1.2-webkit2-4.1"
    fi

    # Check Python dependencies
    echo ""
    echo "  ── Python 依赖 ──"
    PYTHON_BIN="$INSTALL_DIR/venv/bin/python3"
    if [ -f "$PYTHON_BIN" ]; then
        local all_ok=true
        # 从 requirements.txt 读取包名（取每行 = 或 >= 前的部分）
        while IFS= read -r line || [ -n "$line" ]; do
            # 去掉注释和空白
            pkg_line="${line%%#*}"
            pkg_line="${pkg_line%% #*}"
            pkg_line="$(echo "$pkg_line" | xargs)"
            [ -z "$pkg_line" ] && continue
            # 提取包名: "PyGObject>=3.42" → "PyGObject"
            pkg_name="${pkg_line%%>=*}"
            pkg_name="${pkg_name%%==*}"
            pkg_name="$(echo "$pkg_name" | xargs)"
            # 处理特殊情况: python-magic → import 名不同
            import_name="$pkg_name"
            case "$pkg_name" in
                "PyGObject") import_name="gi" ;;
                "Pygments") import_name="pygments" ;;
                "rank-bm25") import_name="rank_bm25" ;;
                "pymdown-extensions") import_name="pymdownx" ;;
                "google-api-python-client") import_name="googleapiclient" ;;
                "google-auth-oauthlib") import_name="google_auth_oauthlib" ;;
                "google-auth-httplib2") import_name="google_auth_httplib2" ;;
            esac
            # 只校验合法导入名；模块名经 argv 传入 importlib，绝不拼入 python 源码
            case "$import_name" in
                [A-Za-z_][A-Za-z0-9_.]*)
                    if py_import_ok "$PYTHON_BIN" "$import_name"; then
                        echo -e "    ${GREEN}✔${NC} $pkg_name"
                    else
                        echo -e "    ${RED}✘${NC} $pkg_name (缺失)"
                        all_ok=false
                    fi
                    ;;
                *)
                    warn "跳过依赖检查: $pkg_name (非法导入名: $import_name)"
                    ;;
            esac
        done < "$SCRIPT_DIR/requirements.txt"
        if [ "$all_ok" = true ]; then
            echo -e "    ${GREEN}全部依赖已安装${NC}"
        else
            echo -e "    ${YELLOW}部分依赖缺失，请运行 install 或手动安装${NC}"
        fi
    else
        echo -e "  Python 虚拟环境: ${RED}未找到 ($PYTHON_BIN)${NC}"
    fi
}

# ── Main ──────────────────────────────────────
case "${1:-install}" in
    install)   validate_install_dir; cmd_install ;;
    uninstall) validate_install_dir; cmd_uninstall ;;
    status)    cmd_status ;;
    help|--help|-h) usage ;;
    *)
        error "未知命令: $1"
        usage
        exit 1
        ;;
esac
