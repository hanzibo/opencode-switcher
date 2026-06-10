# Backup/Restore 功能开发经验总结

> **分支名**：`add-feature-backup`
> **开发周期**：2026-06-10 11:03 ~ 2026-06-10 11:52（约 49 分钟）
> **关键词**：`备份恢复` `GTK3` `FileChooserDialog` `tar.gz` `托盘菜单` `TDD` `代码重构`

## 一、经验与教训总结

### 1.1 做得好的地方

- **TDD 先行**：在编写 UI 代码前先创建独立测试脚本验证 tarfile 打包/解压逻辑，确保核心功能正确后再与 GTK 集成
- **小步提交**：6 次原子提交（core logic → UI wiring → bugfix → menu → refactor），每条符合 Conventional Commits，便于回滚和审查
- **Bug 快速定位**：用户反馈 Backup 无文件生成后，立即定位到 `dlg.destroy()` 在 `dlg.get_filename()` 之前的经典 GTK 陷阱
- **代码审查补全**：在功能完成后主动进行代码质量审查，发现并消除了重复对话框方法的可维护性问题
- **焦点保护一致**：所有新增对话框都正确使用了 `on_dialog_shown`/`on_dialog_hidden` 回调，与项目现有模式一致

### 1.2 需要改进的地方

- **GTK API 调用顺序验证不足**：未在开发阶段发现 `get_filename()` 在 `destroy()` 后的失效问题，应在提交前执行端到端手动验证
- **返回类型风格不一致**：初始实现使用了 `tuple[bool, str]`，与项目约定的 `Optional[str]`（None=成功）风格不符，需在后续代码审查中提前注意项目惯例
- **未能一次完成所有 UI 细节**：Restart 菜单是用户后续提出才补加的，说明前期对还原完整用户体验场景的考虑不够周全

## 二、关键问题与解决方案记录

### 问题1：Backup 无文件生成
- **问题描述**：点击 Backup 按钮 → 选择目录 → 点击确认，但目标目录中无备份文件。
- **原因分析**：在 `_on_backup_clicked` 和 `_on_restore_clicked` 回调中，`dlg.destroy()` 先于 `dlg.get_filename()` 执行。GTK3 中，Widget 销毁后调用其方法返回 `None`，导致选择的路径丢失。
- **解决过程**：
  1. 审查代码定位问题行：`_on_backup_clicked` 回调函数内先 destroy 后 get_filename
  2. 确认同样 bug 存在于 `_on_restore_clicked`
  3. 在回调函数内将 `selected_path = dlg.get_filename()` 移到 `dlg.destroy()` 之前
- **最终方案**：
  ```python
  def on_response(dlg, resp):
      path = dlg.get_filename()   # ← 先捕获
      dlg.destroy()                # ← 再销毁
      ...
  ```
- **预防建议**：GTK 中凡涉及 `destroy()` 后从 dialog 获取属性的操作，必须确保取值在 destroy 之前完成。可建立代码审查清单项：`destroy()` 前检查所有 dialog 属性读取。

### 问题2：还原后需手动重启
- **问题描述**：Restore 完成后，数据文件已还原，但应用仍在运行旧的内存数据，需要手动 Quit 再启动。
- **原因分析**：`load_cached()` 只刷新面板显示，但无法重新加载进程外已变更的文件。完整重启才能确保所有模块使用新数据。
- **最终方案**：托盘菜单添加 "Restart" 选项，内部流程：设标志位 → `stop()` 退出 GTK 主循环 → `__main__` 恢复执行 → 释放 `fcntl.flock` 锁文件 → `subprocess.Popen` 生成新进程。
- **预防建议**：对于需要重置整个进程状态的"还原"类操作，Restart 机制比增量重载更可靠。后续所有类似功能应一并考虑 Restart 入口。

## 三、技术要点沉淀

### 3.1 GTK FileChooserDialog 最佳实践

```python
dialog = Gtk.FileChooserDialog(
    title="Select backup destination",
    transient_for=self.get_toplevel(),
    action=Gtk.FileChooserAction.SELECT_FOLDER,  # 或 OPEN
)
dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
dialog.add_button("_ActionName", Gtk.ResponseType.ACCEPT)
dialog.set_default_response(Gtk.ResponseType.ACCEPT)

# 文件类型过滤（仅 OPEN 时需要）
filt = Gtk.FileFilter.new()
filt.add_pattern("*.tar.gz")
dialog.add_filter(filt)

# 重要：回调内先取值再 destroy
def on_response(dlg, resp):
    path = dlg.get_filename()
    dlg.destroy()
    ...
```

### 3.2 单实例应用重启模式

适用于使用 `fcntl.flock(LOCK_NB)` 实现单实例限制的应用：

```python
# 设标志位 → stop() 退出主循环 → __main__ 检查标志
if app._restart_requested:
    lock_fd.close()                                    # 释放文件锁
    subprocess.Popen([sys.executable] + sys.argv)       # 生成新进程
```

关键点：不能先 spawn 后 stop（新进程因 LOCK_NB 会立即退出），必须 stop 释放锁后再 spawn。

### 3.3 项目代码风格惯例

- **错误返回**：`Optional[str]`（`None` = 成功，`str` = 错误描述），而非 `tuple[bool, str]`
- **对话框焦点保护**：每个模态对话框前后调用 `on_dialog_shown()` / `on_dialog_hidden()` 回调

## 四、后续优化建议

- **还原前自动备份**：Restore 操作前可先自动备份当前状态，方便用户撤销误操作
- **备份文件管理**：可增加列出/删除已有备份文件的功能，避免用户手动清理
- **进度提示**：对于包含大量图片的大备份，可添加进度条或后台线程提示
- **定时自动备份**：可增加可选的定时自动备份配置

## 五、参考资料

- [GTK3 FileChooserDialog API](https://docs.gtk.org/gtk3/class.FileChooserDialog.html)
- [tarfile — Python 官方文档](https://docs.python.org/3/library/tarfile.html)
- [fcntl.flock — 文件锁机制](https://docs.python.org/3/library/fcntl.html#fcntl.flock)
