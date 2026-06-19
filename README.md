# OpenCode Switcher

OpenCode Switcher 是一个专为 Linux GTK3 桌面环境设计的系统托盘应用。它允许用户通过极简的搜索面板，在不同的 OpenCode (CLI) 会话之间快速搜索、管理和切换，并内置了高精度的剪切板历史管理、自定义模板提示词和系统集成功能。

> [!TIP]
> 关于软件各项特性的具体使用细节、全局快捷键映射、多参数模板（Dynamic Copy）以及配置备份与还原等，请参阅 [详细使用指南文档 (docs/usage.md)](docs/usage.md)。

---

## 🌟 主要特性

- **🚀 会话快速切换**：在面板中实时搜索、重命名和删除 OpenCode 历史会话。智能识别并标出当前正在运行（Live）的会话。
- **📋 高精度剪切板历史**：
  - 基于混合启发式算法，自动对剪切板复制内容分类为 `文本`、`链接`、`代码` 和 `图片`。
  - 自动识别代码片段的具体编程语言（如 Python、JavaScript、Shell、C++、Java、SQL 等），并在 UI 列表中直观呈现大写的语言标签。
- **✍️ 自定义提示词与占位符工程**：
  - 自定义分类提示词管理，支持对历史项执行快捷 AI 搜索与分析。
  - 支持模板化占位符 `${&}`，可在提示词中任意位置内嵌剪切板原内容；支持反斜杠转义（`\${&}` 将输出为字面量）。
  - Prompts Config 弹窗提供快捷置入 `+ ${&}` 按钮。
- **🖥️ 平台双模适配 (X11 & Wayland)**：
  - **X11**：后台守护线程毫秒级轮询剪切板，使用 `pynput` 监听全局快捷键 `Ctrl+Shift+Space`，利用 `xdotool` 执行窗口聚焦。
  - **Wayland**：完全停用后台轮询，通过配套的 **GNOME Shell 扩展** 实时监听剪切板变动，通过系统级 Unix 套接字监听快捷键触发，通过文件共享机制安全请求窗口聚焦。
- **🔧 智能终端拉起**：自动探知系统中安装的终端（按优先级：`Ptyxis` → `GNOME Terminal` → `Console/kgx` → `Black Box`），并在对应的终端里拉起指定的 OpenCode 会话。

---

## 🛠️ 准备工作与依赖安装

应用运行需要 Python 3、GTK3 绑定以及相关系统工具。请在安装前运行以下命令安装系统依赖：

```bash
# Debian/Ubuntu 及其衍生系统
sudo apt install gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pip python3-venv wl-clipboard xclip xdotool
```

> **注意**：`xdotool` 为可选安装，仅在 X11 环境下执行窗口强聚焦时需要。

---

## 🚀 安装步骤

1. 克隆本项目仓库：
   ```bash
   git clone https://github.com/your-username/opencode-switcher.git
   cd opencode-switcher
   ```

2. 运行一键安装脚本（此脚本将自动初始化虚拟环境 `venv`、安装依赖、部署应用至本地目录，并注册并启用 Systemd 用户服务和 GNOME Shell 扩展）：
   ```bash
   ./install.sh install
   ```

3. **快捷键绑定（仅限 Wayland 环境）**：
   由于 Wayland 下的安全限制，全局快捷键需要手动在 GNOME 设置中绑定：
   - 打开 **系统设置** → **键盘** → **查看及自定义快捷键** → **自定义快捷键**。
   - 新增一条快捷键：
     - **名称**：`OpenCode Switcher`
     - **命令**：`opencode-switcher-toggle`
     - **快捷键**：`Ctrl+Shift+Space` (或任何您喜欢的组合键)

---

## 📂 项目结构

- `main.py` - 应用主入口，处理单实例锁、托盘指示器创建及主循环。
- `panel.py` - 核心 GTK3 搜索面板窗口，处理会话列表渲染、主题应用和输入交互。
- `clipboard_panel.py` - 剪贴板历史与自定义分类分栏面板，处理历史项展示和 Prompts 模板配置。
- `clipboard_store.py` - 负责剪贴板条目加载、去重、启发式分类与语言自动探测。
- `session_store.py` - 读取 OpenCode 本地 SQLite 数据库，并匹配 `/proc` 进程信息检测活体状态。
- `hotkey.py` - 兼容 X11 (pynput) 与 Wayland (Unix Socket) 的热键管理模块。
- `launcher.py` - 系统终端的自动检测与拉起运行逻辑。
- `utils.py` - 包含相对时间处理和跨平台聚焦请求的公共工具函数。
- `gnome-extension/` - GNOME Shell 扩展，负责在 Wayland 下监听剪切板 owner 变更并向应用发送通知。

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 开源协议。
