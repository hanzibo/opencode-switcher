# Review Plan: AI Panel Splitter See-Through Fix

## 审查摘要

分支 `fix/ui-display-issue`（3 个提交：`e45fb38`、`a2cbcf6`、`30195f6`）修复了拖动分栏器时 AI 面板区域背景透穿问题。
根因：`e94828e` 为圆角把 `set_opaque_region(None)`，导致 Wayland 合成器对 ARGB 窗口做半透明混合，
resize 瞬间采样未初始化像素露出桌面。最终修复：恢复 opaque region 并适配为圆角矩形。

审查发现整体质量良好（根因正确、防御分层、降级保护），但有 1 处冗余性能开销与若干可维护性小问题。

## 问题清单（含优先级）

| # | 优先级 | 位置 | 问题 | 处理 |
|---|--------|------|------|------|
| 1 | 中 | `views/clipboard_panel.py` `_on_content_paned_position_changed` + `notify::position` 连接 | opaque region 已根治透穿，拖动时每帧全窗口 `queue_draw()` 为冗余防御，且存在卡顿风险 | 删除 |
| 2 | 中 | `views/panel.py` `_update_opaque_region` vs `_on_window_draw` | 圆角路径几何完全重复，改半径/offset 需两处同步 | 提取共用辅助函数 |
| 3 | 中 | `views/clipboard_panel.py` CSS `#aiScrolled, #aiWebView` | 依赖 `dialog_bg == ai_bg` 隐性等价，未来主题若不同会出现色差 | 加注释说明假设 |
| 4 | 低 | `views/ai_chat_panel.py` `_apply_webview_gtk_background` | `except Exception: pass` 静默吞异常，与 panel.py 降级风格不一致 | 加调试日志 |
| 5 | 低 | `views/panel.py` | `import logging` 在 except 块内局部导入 | 移到模块顶部 |
| 6 | 低 | `views/ai_chat_panel.py` | `getattr(self, "_ai_webview", None)` 防御写法与项目风格不一致 | 改直接属性访问 |

## 分步修改方案

1. **clipboard_panel.py**：删除 `notify::position` 连接（含注释）与 `_on_content_paned_position_changed` 方法
2. **panel.py**：提取模块级函数 `_rounded_window_path(cr, w, h)`，`_on_window_draw` 与 `_update_opaque_region` 共用；`import logging` 移到顶部
3. **clipboard_panel.py**：CSS 行加等价性注释
4. **ai_chat_panel.py**：`except` 加 `logging.debug`；`getattr` 改直接属性访问

## 验证方法

- `venv/bin/python3 -m py_compile views/panel.py views/ai_chat_panel.py views/clipboard_panel.py`
- `venv/bin/python3 -m unittest discover tests`（预期 624 OK / 2 skipped）
- 实机：重启应用 → 快速左右拖动分栏器 → 确认①无透穿 ②无卡顿 ③圆角正常 ④主题切换后 AI 面板背景色一致

## 回滚思路

- 本轮为纯重构/清理，不改变修复行为；若实机异常，`git checkout views/` 恢复即可
- 分支各提交相互独立，后续如需回退功能可用 `git revert <sha>`
