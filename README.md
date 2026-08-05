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
- **🖥️ Wayland 原生支持**：通过配套的 **GNOME Shell 扩展** 实时监听剪切板变动（`owner-changed` 信号），通过系统级 Unix 套接字监听快捷键触发，通过文件共享机制安全请求窗口聚焦。
- **🔧 智能终端拉起**：自动探知系统中安装的终端（按优先级：`Ptyxis` → `GNOME Terminal` → `Console/kgx` → `Black Box`），并在对应的终端里拉起指定的 OpenCode 会话。
- **🤖 AI 助手侧栏**：内嵌 WebKit2 WebView 的多轮 LLM 对话面板，支持流式输出、Markdown/代码高亮/KaTeX 数学渲染、图片附件、模型切换，以及基于 ReAct 循环的 9 种工具调用（网页搜索、文件操作等）。
- **🔁 对话回滚与重试**：支持回滚到任意历史轮次（`/rollback`），重试上一轮响应（`/retry`），以及完整的对话历史管理。

---

## 🛠️ 准备工作与依赖安装

应用运行需要 Python 3、GTK3 绑定以及相关系统工具。请在安装前运行以下命令安装系统依赖：

```bash
# Debian/Ubuntu 及其衍生系统
sudo apt install gir1.2-ayatanaappindicator3-0.1 python3-gi python3-gi-cairo python3-pip python3-venv wl-clipboard gir1.2-webkit2-4.1
```

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

```
./
├── main.py                     # 应用主入口：单实例锁、托盘指示器、Gtk.main()
├── views/                      # 主 UI 视图
│   ├── panel.py                # 搜索面板：会话搜索、斜杠命令、CSS 主题
│   ├── clipboard_panel.py      # 剪贴板面板容器：组装子组件 + 事件路由
│   ├── ai_chat_panel.py        # AI 助手侧栏：WebView、LLM 对话、流式输出
│   └── ai_popovers.py          # AI 命令自动补全 + 历史对话弹窗
├── dialogs/                    # GTK 对话框（设置/提示词/回收站/排序/记忆管理等）
├── stores/                     # 数据持久化层
│   ├── clipboard_store.py      # 剪贴板去重/分类、自定义分类、对话存储
│   ├── session_store.py        # OpenCode SQLite 数据库读取 + 进程活体检测
│   ├── skill_store.py          # AI Skill 存储
│   └── theme_config.py         # 主题配置
├── ai_engine/                  # AI LLM 引擎与渲染
│   ├── llm_client.py           # LLM HTTP 客户端（SSE 流式、工具调用累积器）
│   ├── ai_tool_loop.py         # ReAct 工具调用循环
│   ├── ai_html_template.py     # WebView HTML 模板 + KaTeX 内联
│   └── render_pipeline.py      # 渲染管线
├── ai_text_utils/              # 纯文本/Markdown/数学/图像工具（零 GTK 依赖）
├── mcp_integration/            # MCP 协议层（JSON-RPC over stdio/http、GTK asyncio 桥）
├── tool_registry/              # AI 工具执行器（28 个工具：bash/web/文件/子代理等）
├── html_templates/             # WebView 前端资源（chat.js / chat.css）
├── system/                     # 系统 IPC 与工具
│   ├── hotkey.py               # 热键管理：Wayland (Unix Socket)
│   ├── launcher.py             # 终端自动检测 + OpenCode 会话拉起
│   ├── utils.py                # 工具函数：相对时间、聚焦请求、缓存路径
│   ├── migrate_history.py      # 数据库迁移工具
│   └── inspect_db.py           # 数据库检查器
│
├── opencode-switcher-toggle    # Unix Socket 触发脚本
├── katex/                      # KaTeX CSS/JS/字体（AI WebView 数学渲染）
├── gnome-extension/            # GNOME Shell 扩展（Wayland 剪贴板 + 聚焦 IPC）
├── deploy/                     # systemd service / desktop 模板（install.sh sed 替换）
├── docs/usage.md               # 详细使用指南
├── run.sh                      # 生产环境启动器（日志轮转、nvm）
├── install.sh                  # 安装/卸载/状态脚本
└── requirements.txt            # Python 依赖
```

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 开源协议。
