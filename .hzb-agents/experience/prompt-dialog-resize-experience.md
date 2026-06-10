# 提示词编辑对话框自适应拉伸 性能优化与经验总结

> **分支名**：`improve-feature-edit-prompt-dialog`  
> **开发周期**：2026-06-10 至 2026-06-10  
> **关键词**：`组件大小自适应` `Gtk.Box 打包规则` `单例锁冲突` `系统服务重启`

## 一、经验与教训总结

### 1.1 做得好的地方
- **精准的 GTK3 布局定位**：快速定位到 `Gtk.Box` 的默认 `add()` 方法打包规则导致的组件拉伸限制，用最少的一行代码改动（最小侵害原则）完美解决了窗口拉伸时输入框高度固定的问题，保证了系统级极高的稳定性。
- **系统层问题排查**：在部署后发现服务未能正常启动时，没有盲目修改代码，而是根据退出码 `0/SUCCESS` 结合单例锁逻辑，迅速定位到了后台多实例冲突（Legacy Process 占用 Lock）并予以清理，展现了系统级的调试思维。

### 1.2 需要改进的地方
- **服务部署生命周期脱节**：在运行 `./install.sh install` 重新编译安装应用时，安装脚本并没有主动清理已经在外部会话中运行的遗留 `opencode-switcher` 进程。这导致了单例锁（`flock`）被遗留进程占用，使得 systemd 新服务因冲突而启动失败。后续应当在安装脚本或部署命令中加入遗留进程的杀除逻辑。

---

## 二、关键问题与解决方案记录

### 问题1：Edit Prompt 对话框垂直拉伸时 Text 输入框高度不变
- **问题描述**：拖拽 `Edit Prompt` / `Create Prompt` 对话框调整其大小时，文本输入区 `TextView` 仅能横向展宽，其高度始终固定不变，无法充分利用拉伸后的窗口空间。
- **原因分析**：
  在 [clipboard_panel.py](file:///home/hzb/opencode-switcher/clipboard_panel.py) 中，文本区域所在的滚动窗口 `sw` 使用 `content.add(sw)` 添加到垂直盒布局中。GTK3 的 `add` 容器方法对 `Gtk.Box` 默认使用的是 `expand=False` 打包，从而限制了子组件随父容器自适应拉伸的能力。
- **解决过程**：
  将 `content.add(sw)` 替换为 `content.pack_start(sw, True, True, 0)`。
- **最终方案**：
  使用 `pack_start` 显式声明 `expand=True` 和 `fill=True`，让滚动窗口 `sw` 获得在垂直方向自动扩张并填满剩余空间的权限。
- **预防建议**：
  在 GTK 开发中，向 `Gtk.Box` (如 `Gtk.VBox` 或 `Gtk.HBox`) 添加需要随窗口拉伸而改变尺寸的核心输入控件（如 TextView, ScrolledWindow, TreeView 等）时，应避免使用 `add()`，一律采用 `pack_start(..., True, True, 0)` 显式指定拉伸参数。

### 问题2：安装部署后 systemd 用户服务处于 dead 状态（退出码 0）
- **问题描述**：修改完代码执行 `./install.sh install && systemctl --user restart opencode-switcher` 后，状态检查发现服务为 `inactive (dead)`，但退出状态显示为 `0/SUCCESS`。
- **原因分析**：
  程序在初始化时使用文件排他锁（`fcntl.flock`）防止多实例运行。当前桌面会话中已经启动了一个遗留的主程序进程（PID `74478`，可能是开机自启或前次未清理干净的进程），且它处于 systemd 的进程树控制范围之外。新启动的 systemd 实例由于无法获取锁，因此按照单例守卫的设计干净地退出了（Exit Code 0）。
- **解决过程**：
  1. 运行 `ps aux | grep opencode-switcher` 定位到持有锁的遗留进程 PID `74478`。
  2. 运行 `kill 74478` 释放锁文件占用。
  3. 执行 `systemctl --user restart opencode-switcher` 成功拉起新服务。
- **最终方案**：
  杀除所有控制组外的残留程序实例，重新启动 systemd 守护服务。
- **预防建议**：
  在发布或重新部署具有文件锁（`flock`）、端口监听等单例保护的桌面工具时，部署流程中必须先通过名称/路径强制杀除历史多余进程（如 `pkill -f`），再重新引导守护服务。

---

## 三`、技术要点沉淀

- **GTK3 盒子打包参数**：
  在 `Gtk.Box` 布局中：
  - `add(widget)` $\rightarrow$ `pack_start(widget, expand=False, fill=True, padding=0)`
  - `pack_start(widget, True, True, 0)` $\rightarrow$ 支持宽度与高度双向自适应拉伸。
- **排查单例锁死锁**：
  单例守护退出（一般返回 0）是系统服务排查中容易忽视的死灰复燃现象。需要检查排他锁路径（通常是 `/tmp/opencode-switcher.lock` 或类似路径）的属主与进程运行环境。

---

## 四、后续优化建议

- **改进安装与部署脚本**：
  在 [install.sh](file:///home/hzb/opencode-switcher/install.sh) 的 `install` 命令或 `run.sh` 中，加入在启动前扫描并清理其它非 systemd 控制组内残留 python 实例的逻辑，防止部署后由于单例锁冲突导致服务无法启动。

---

## 五、参考资料

- [GtkBox Layout packing documentation](https://docs.gtk.org/gtk3/method.Box.pack_start.html)
- [Python fcntl Lock Documentation](https://docs.python.org/3/library/fcntl.html)
