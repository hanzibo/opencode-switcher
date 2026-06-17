# Ponytail 项目代码极简优化经验总结

> **分支名**：`optimize-project-ponytail`  
> **开发周期**：2026-06-17 至 2026-06-17  
> **关键词**：`代码简化` `YAGNI` `死代码清理` `Ponytail工具` `代码审计`

## 一、经验与教训总结

### 1.1 做得好的地方
- **坚决贯彻 YAGNI 原则**：在重构和优化中，摒弃了“以后可能会用”的防御性编程心态，删除了所有已失去使用场景的陈旧数据结构与接口层，使核心代码更加精简和纯粹。
- **全案过度设计审计 (Ponytail Audit)**：借助 ponytail-audit 工具扫描项目全树，系统化地排查出无用参数、死变量、重复封装等五类优化点，排查出 80 余行可删代码，显著降低了系统的维护负荷。
- **重构后闭环测试与部署**：在清理完多处核心类的入参（如 `prompt_store`）后，通过 `py_compile` 编译验证、21 项类型分类单元测试，以及实际重新部署服务，确保优化期间 100% 保持了功能正确性。

### 1.2 需要改进的地方
- **前期重构遗留清理不够彻底**：在自定义类别（Categories）特性开发时，明知 `PromptStore` 已被完全替代，但为了“导入兼容性”而选择保留在代码库中。未来在开发新特性时，应当在特性分支内一步到位地完成对旧机制的彻底清理，而不是留作技术债务。

## 二、关键问题与解决方案记录

### 问题1：旧版 Prompt 管理机制被废弃后遗留的死代码与假依赖
- **问题描述**：在引入自定义分类（CategoryStore）后，旧的 PromptStore 与 Prompt 类，以及 `prompts.json` 路径变量已不再发挥作用，但在 `main.py`、`panel.py` 和 `clipboard_panel.py` 中仍然存在该对象的实例化、构造传参与局部属性赋值。
- **原因分析**：重构时为规避修改多处初始化传参的风险，采取了保守的“保留死代码以兼容”策略，形成了无意义的耦合。
- **解决过程**：
  1. 在 [clipboard_store.py](file:///home/hzb/opencode-switcher/clipboard_store.py) 中移除 `PromptStore`、`Prompt` 类定义及 `PROMPTS_PATH` 变量与 `migrate_from_prompts` 迁移方法。
  2. 修改 [main.py](file:///home/hzb/opencode-switcher/main.py) 的 `App` 初始化，停止实例化 `PromptStore` 并移除在面板初始化时的入参。
  3. 精简 [panel.py](file:///home/hzb/opencode-switcher/panel.py) 中 `set_clipboard_panel` 的参数接收，并移除 `self._prompt_store` 赋值。
  4. 精简 [clipboard_panel.py](file:///home/hzb/opencode-switcher/clipboard_panel.py) 中 `ClipboardPanel` 构造函数接收参数。
- **最终方案**：
  彻底废除 PromptStore 的整个生命周期，从依赖树中完全剥离。
- **预防建议**：
  新特性替换旧特性后，不要采取妥协保留方案。如果架构发生变更，应确保关联类及其构造函数的入参同步重构，保持接口的简洁性。

### 问题2：`HotkeyManager` 中残留无意义的按键集合属性
- **问题描述**：在 [hotkey.py](file:///home/hzb/opencode-switcher/hotkey.py) 的 `_start_pynput` 方法中，定义了 `self._pynput_keys` 并将其赋值为特定按键集合，但在方法尾部又直接将其设为 `None`，过程中没有被任何其他逻辑读取。
- **原因分析**：历史版本中用于做按键白名单过滤的属性，在重构为硬编码正则/集合判定后被遗漏清除。
- **解决过程**：
  在 `_start_pynput` 方法中删除了 `self._pynput_keys` 的声明与重置语句。
- **最终方案**：
  移除了全部无用的按键白名单死代码。
- **预防建议**：
  编写代码时应借助 IDE 的静态分析或 Lint 工具，及时发现并清理“已赋值但从未读取（Assigned but never accessed）”的本地变量或实例属性。

### 问题3：`launcher.py` 中冗余的平台检测包装函数
- **问题描述**：[launcher.py](file:///home/hzb/opencode-switcher/launcher.py) 定义了局部私有函数 `_on_wayland` 仅用于封装调用 `is_wayland()`，属于无谓的“套娃”封装。
- **原因分析**：在开发初期为了保持模块私有属性而设计的冗余抽象。
- **解决过程**：
  1. 删除 `_on_wayland` 的定义。
  2. 在 `_launch` 方法中直接调用 `is_wayland()` 判定环境。
- **最终方案**：
  内联了所有的 `_on_wayland()` 判定，直接依赖 `utils` 模块公开的标准方法。
- **预防建议**：
  尽量避免为单行简单逻辑或纯代理调用编写本地辅助函数。代码层级越少，阅读时的认知负荷越低。

## 三、技术要点沉淀

- **极简重构（YAGNI）开发规范**：
  在模块优化中，应优先通过 ponytail-audit 确立废弃范围清单。清理死代码时，先清理数据层定义，再逆向追溯 UI 交互层参数，最后执行编译检查。
- **带标记的优化注释**：
  引入 `# ponytail: ...` 注释标记所有因精简、内联或删除而做出的架构调整，便于后续开发者追溯优化动机。
  ```python
  # ponytail: removed unused self._prompt_store = PromptStore()
  ```

## 四、后续优化建议

- **移除未跟踪元数据**：清理 `.omo/` 下与已废弃 Prompt 关联的历史废弃设计文档，保持开发状态树轻量。
- **依赖库审计**：若后期完全在 Wayland 下工作，可以考虑通过可选依赖（extras）机制将 X11 专用的 `pynput` 设为条件安装，进一步精简安装包依赖。

## 五、参考资料

- 《重构：改善既有代码的设计》中关于“冗余类（Lazy Class）”和“夸夸其谈未来性（Speculative Generality）”的重构准则。
