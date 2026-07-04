# 优化 AI 看盘图片显示机制 开发经验总结

> **分支名**：`optimize-ai-image-display`  
> **开发周期**：2026-07-04 至 2026-07-04  
> **关键词**：`AI看盘` `图片显示` `灯箱模式` `拖动平移` `滚轮缩放` `WebKit性能优化` `f-string转义`

## 一、经验与教训总结

### 1.1 做得好的地方
- **交互设计完整性**：在有限的单文件结构中，用纯前端 JavaScript + CSS 实现了一套体验流畅、功能闭环的 Lightbox 方案（卡片缩放 -> 全屏遮罩 -> 滚轮缩放 -> 抓手平移 -> 双击重置 -> 点击背景关闭），在完全不引入外部包的前提下，达到了媲美专业 Web 客户端的交互效果。
- **性能与动效兼顾**：通过动态增删 `.dragging` 类来切分“平移”与“缩放”状态下的动画过渡属性，解决了流畅手感与平滑视觉效果的潜在冲突。
- **帧合并节流**：使用 `requestAnimationFrame` 限制 DOM 写入频次，在无 JIT（`JSC_useJIT=false`）的 WebKit 嵌入环境下实现了 60fps 级别的顺滑平移体验。

### 1.2 需要改进的地方
- **f-string 字符冲突敏感度不足**：在第一次将 JS 模板字符串写入 `ai_html_template.py` 时，忽视了 Python f-string 编译期与运行期的执行差别，引发了运行时 `NameError`。对于返回 f-string 模板的 Python 代码，所有 JS 中的 `{}` 花括号写法必须刻进肌肉记忆双重化 `{{}}` 转义。
- **静态测试的局限性**：Gtk Webview 中的 JS 语法错误在 `python3 -m py_compile` 时不会报错，甚至在服务拉起初期也不会立刻报错，直到 Webview 执行模板渲染时才发生 KeyError。后续凡是修改 HTML 模板文件，必须手动通过交互式 Python 运行一次模板函数以确保格式化安全。

---

## 二、关键问题与解决方案记录

### 问题1：大图在 400px 限制下细节丢失，且无法放大查看
- **问题描述**：原来所有的图片均硬编码了 `style="max-width:400px;"`，对于长宽比极大的截图（如 16:9 桌面截图），等比缩放后高度仅有百余像素，细节彻底模糊且无法操作。
- **原因分析**：缺乏专门的看图容器 and 缩放机制。
- **解决过程**：
  1. 移除 `<img>` 的行内 `max-width` 限制，统一改用 CSS 类 `.chat-image` 封装缩略图。
  2. 在 Webview 模板 of HTML 结构中，添加全局悬浮模态遮罩层 `#lightbox` 及承载大图的 `#lightbox-img`。
  3. 通过 `onclick="showLightbox(this.src)"` 事件将图片装入灯箱。
- **最终方案**：采用内置遮罩层（Lightbox）方案，支持自适应放大，鼠标悬停时提供轻微放大及 `zoom-in` 手势引导。
- **预防建议**：涉及大量文本/代码分析的 AI Chat 界面，图片展示必须提供大图预览功能，尤其是处理用户上传的系统截图。

### 问题2：图片选择器（添加图片链接）拉起时，大窗口失焦自动隐藏
- **问题描述**：在主面板上点击 `📎` 按钮打开文件选择对话框时，整个主面板意外隐藏，无法保持显示。
- **原因分析**：`panel.py` 的失焦隐藏事件 `_on_focus_out` 会检查 `self._dialog_active` 状态。虽然 `ClipboardPanel` 提供了回调钩子，但是在主面板实例化 `ClipboardPanel` 并注入钩子时，`ClipboardPanel` 内部早已静态构建完成了 `AIChatPanel`，导致注册的回调函数没有向下同步传递给 AI 聊天面板。
- **解决过程**：
  - 将 `ClipboardPanel` 中对外部注入的 10 个回调钩子变更为 `@property` + `@setter` 动态监听机制。
  - 在外部设置回调时，通过 Setter 隐式地将最新的回调广播同步给底层嵌套的 `self._ai_chat_panel`。
- **最终方案**：利用 Python 动态属性代理，解决了嵌套 Widget 在异步生命周期中回调函数捕获为 `None` 的问题。
- **预防建议**：对于层级较深的 GTK 组件，如果存在异步/运行时动态绑定的事件钩子，应使用 Setter 广播器或信号槽机制，避免静态捕获。

### 问题3：JS 模板字符串 `${variable}` 在 Python 字符串模板中引发 NameError
- **问题描述**：写入 JS 拖拽代码 `img.style.transform = \`scale(${lightboxScale})\`` 时，服务在运行期解析模板时崩溃。
- **原因分析**：Python f-string 使用 `{}` 占位。JS 的 `` `scale(${variable})` `` 在 Python 看来等价于一个名为 `$variable` 的 Python 表达式，格式化时找不到对应 Python 变量进而报错。
- **解决过程**：将所有包含 JS 变量插值的花括号更改为双花括号：`\`scale(${{lightboxScale}})\``。
- **最终方案**：通过双花括号转义 Python f-string 占位符。
- **预防建议**：尽量减少 Python 和 JavaScript 语言插值占位符在大段多行文本中的混合。若必须混合，所有的 JS `{}` 都要确保双重包裹。

### 问题4：拖拽图片严重滞后不跟手
- **问题描述**：在按住左键平移大图时，图片滞后于鼠标指针约 `0.2` 秒，有明显的阻尼感。
- **原因分析**：图片的 CSS class 上硬编码了 `transition: transform 0.2s ease`。当鼠标微移触发 transform 更改时，浏览器强行启动 0.2 秒动画插值过渡，导致拖拽轨迹无法实时对齐。
- **解决过程**：
  1. 新建 `.lightbox-img.dragging {{ transition: none !important; }}` CSS 规则。
  2. 在 `mousedown` 时为大图加上 `dragging` 类，在 `mouseup` 释放时移除，使平移阶段达到物理零延迟。
  3. 用 `requestAnimationFrame` 封装 `mousemove` 的 DOM 更新逻辑，避免短时间内进行上百次无效的重绘。
- **最终方案**：rAF 渲染合并 + 拖拽态动画强制屏蔽。
- **预防建议**：实现任何拖拽、平移或频繁跟随鼠标坐标改变样式的操作时，**必须确保该元素上没有任何 transition 动画**，并优先使用 `requestAnimationFrame` 保证性能。

---

## 三、技术要点沉淀

- **CSS/JS 无动画平移抓手模式**：
  适用于任何需要在嵌入式 Webview 中展示大图并允许拖拽缩放的极简场景（零外部库依赖）。
- **Python-JS 二维占位符转义规范**：
  在 Python 文件中返回包含 JS 代码的 f-string 时，需使用以下规则：
  - CSS 规则：`.class {{ margin: 0; }}` (使用双重花括号)
  - JS 普通块：`if (x) {{ foo(); }}` (使用双重花括号)
  - JS 模板字符串插值：`` `translate(${{x}}px)` `` (使用双重花括号包裹 JS 变量名)
  - Python 插值：`color: {theme_color};` (使用单花括号引入 Python 变量)

---

## 四、后续优化建议

- **平移边界边界框限制（Boundary Clamp）**：目前平移大图时允许无限制地拖出屏幕之外，未来可增加可视区域边界计算，限制平移范围，防止大图被彻底拖丢。
- **多触点手势支持**：若后续在触控屏 Linux 设备上运行，可考虑支持两指捏合（Pinch-to-zoom）手势。

---

## 五、参考资料

- WebKit2 Webview 渲染手册与性能优化指南。
- `requestAnimationFrame` 浏览器渲染合并规范。
