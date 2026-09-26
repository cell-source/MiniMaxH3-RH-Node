# 给 Codex 的任务提示词：第二轮上线前审查（交互收敛 + 迁移 v2）

> 使用方式：把本文件全文作为任务提示词投喂给 Codex，让它在仓库根目录（d:\WorkStation\ProjectsVSCode\MiniMaxTool\MiniMaxH3-RH-Node）下工作。
> Codex 只做审查与报告，**不要修改任何代码**。

---

## 角色

你是资深 ComfyUI 插件与全栈代码审查员，熟悉 JavaScript ESM / aiohttp 服务端 / Python 并发 / 工作流序列化兼容问题。任务是**第二轮上线前审查**：上一轮审查（`docs/上线前审查报告-2026-09-25-codex.md`，S1/S2/M1–M6/L1/L2）的修复已合入（`cb4af0c`），之后 UI 交互又经历了一轮大改。本轮审查这批新改动，找出真实缺陷并给出可执行修复建议。

## 项目背景

- ComfyUI 自定义节点包（RunningHub 平台发布）：单节点 `MiniMaxH3IntegrationRH`（`video_nodes.py`），面板 UI 为 `web/js/minimax_h3_integration.js`（单文件 ESM，CSS 内嵌模板字符串）。
- 提示词引擎：`prompt_engine/`（quickjs 沙箱）；Python 出口 `prompt_runtime.py` + `llm_client.py`（`LLMConfigurationError`/`LLMServiceError` 安全异常）。
- 服务端路由 `prompt_optimizer.py`：`/rh/minimax-h3/prompt-optimizer/*`（config/models/optimize/start/status/cancel/generate/format）；新增 `_GENERATION_REQUESTS` 并发注册表（上限 4，含 cancel_event 与断连检测）。
- 隐藏 widget（`prompt_source`/`ai_text`/`ai_*` 约 19 个）不渲染但序列化；`video_nodes.py` execute 的 `prompt_source != "panel"` 分支消费它们。

## 审查范围（git 范围 `cb4af0c..HEAD`，21 个提交）

上一轮修复之后，交互模型经历了三次用户驱动的收敛，**当前最终形态**是：

### A. 动作与配置彻底分离
- 工具条 = 全部执行动作：`↻ 恢复` / `✦ 润色`（再点=取消）/ `≡ 离线整理` / `✨ AI 生成` / `⚙ 配置`
- 配置界面（⚙）纯配置，只有 取消/保存 两个按钮
- `≡`/`✨` 按已保存配置直接执行并填回编辑器（`runGeneration(operation)`，面板级函数）；`✨` 缺 Key 时 `ensureGenerationKey()` 引导打开配置
- 生成/整理/润色进度统一走 `elapsedPrompt`（可点击取消按钮）

### B. 润色（✦ 子功能）完全沿用主区生成配置（`0deb98b`）
- 润色专属的平台/Key/模型/端点/输出语言/本地视觉模型配置**全部删除**；润色子分组只剩「读取视觉素材」「运行前自动润色」两个行为开关
- `runPromptOptimization` 的 `polishConfig` **运行时从主区 widget 构造**：provider=`ai_provider`、key=`getSharedKey(provider) || ai_api_key`、model/endpoint=`ai_model`/`ai_endpoint`、`output_language` 跟随 `ai_language`（zh→中文，mixed/en→English）、`mode: "api"`（local 模式已整体移除）
- `updateWorkflowState`：工作流启动即取消在途润色（不再有 local 条件）
- 后端 `/optimize` 的 config 由前端拼装；RunningHub 托管润色与本地视觉模型模式已不可用

### C. 来源前置选择删除，编辑器内容永远优先（`f30a0c5`）
- 配置界面「来源」下拉删除
- 迁移 v2（`migrateGenerationOptions`，`generationOptionsVersion=2`）：旧工作流的 `ai/offline/format` 来源一律归一为 `panel`
- `mirrorSourceIdea()` 简化为**永远**镜像 `ai_text = prompt.value`（编辑/撤销/填回/恢复各处调用）；`useEditorPrompt` 已删除，改为 `syncEditorSource()`
- 前端永远写 `prompt_source="panel"`；后端 `!= "panel"` 分支保留（widget 仍序列化）

### D. 引擎修复（`bffa8a5`/`e70b3ff`）
- `prompt_engine/rules.js`：新增 DIALOGUE_COUNT REPAIR（缺首行控制行自动纠错 ≤2 次）；`normalizeFieldInline` 支持字段名内联在句末的拆分；`normalizeNegatedSoundFields`（否定式配乐描述→N/A）；固定镜头运动词检测支持否定列表（"不做任何推拉摇移、变焦"）

### E. 文案精简（`23216ff` 等）
- 用户手动定案：UI 标签不带括号说明（"生成平台"/"生成语言"/"H3 模式"）；三段解释性 hint 全部删除

改动统计：`web/js/minimax_h3_integration.js`（±711 行）、`locales/*`、tests 多处。

## 必读文件（按优先级）

1. `web/js/minimax_h3_integration.js` — 重点：`openOptimizerSettings`（分区结构，`beginSection`/`sectionBox` 机制）、`runGeneration`/`generationBody`/`applyGenerationResult`/`ensureGenerationKey`、`runPromptOptimization`（polishConfig 构造 + local 移除后的守卫）、`migrateGenerationOptions`（v2 归一）、`mirrorSourceIdea`/`syncEditorSource`、`applyDialogConfig`（精简后）
2. `prompt_engine/rules.js` — DIALOGUE_COUNT REPAIR 循环（在 LANGUAGE REPAIR 之后）、`normalizeFieldInline` 内联拆分、`normalizeNegatedSoundFields`、`customOptionError` 否定列表
3. `prompt_optimizer.py` — `_GENERATION_REQUESTS` 生命周期、`_generation_api` 断连检测、`_run_prompt_optimization`（output_language 消费）
4. `video_nodes.py` — `execute` 的 `prompt_source != "panel"` 分支（现前端永远写 panel，此分支还可能被谁触发？）
5. tests：`panel-ai-section.cjs`（259）、`panel-generation.cjs`（行为级 host 沙箱）、`test_panel_runtime.py`（AST+quickjs 全链）、`test_generation_api.py`

## 重点审查清单

### A. 交互收敛后的正确性
- **润色沿用主配置**：`polishConfig` 从 widget 构造时，`getSharedKey(polishProvider) || ai_api_key` 双路取值是否在所有路径都有值；`output_language` 映射（zh→中文/mixed→English）与后端 `_normalize_config` 的解析是否闭环；`protocol: "openai"` 硬编码是否对所有生成平台成立（Gemini 等非 openai 协议平台被选为生成平台时润色会怎样？）
- **本地视觉模型移除**：`loadOptimizerSettings` 仍会返回 defaults 里的 local_model 等字段；旧工作流序列化里 `mode: "local"` 恢复后会发生什么（`optimizerSettings` 被 applyDialogConfig 覆盖前的一次润色？）；`configuredOptimizerName` 的 providerName 映射是否覆盖全部生成平台
- **`beginSection`/`sectionBox` DOM 机制**：分区 body 归属是否在所有分支正确（`row()` 闭包引用 `sectionBox` 变量，重赋值时序）；`escapeDialog`/`closeOptimizerSettings`/`refreshDialogActions` 生命周期

### B. 来源删除后的兼容
- 迁移 v2 归一：`migrateGenerationOptions` 在 `onConfigure`/首次 rAF 双路径调用的幂等性；旧工作流 `prompt_source="format"` 且 `ai_text` 为空时恢复流程
- `mirrorSourceIdea` 永远镜像：`applyOptimizedPrompt`/`restorePromptSnapshot`/`resetPrompt.onclick`/`commitPromptEditorInput`/`beforeinput` insertParagraph/恢复流程 六处调用是否完备；`ai_text` 与多模式 promptByMode 的交互（text_keyframes 与 all_reference 切换时镜像哪个？）
- 后端 `prompt_source != "panel"` 分支现在**理论上不可达**（前端永远写 panel），但 widget 可被工作流 JSON 手工构造——评估这条路径是否仍是合理的兼容面

### C. 并发/取消/生命周期（上一轮 M2–M4 修复在新交互下的回归）
- 工具条三动作（✦/≡/✨）+ 配置界面打开状态下点 ✦：`beginPromptOperation` 锁 + `refreshDialogActions` 禁用是否覆盖全部组合
- 工作流启动即取消在途润色（无条件了）：与"运行前自动润色"开关的先后关系
- 节点删除/图切换清理（`panelRemoved`/`settingsEpoch`）在精简后的 applyDialogConfig 下仍完整

### D. 安全（上一轮 M6 修复的持续有效）
- `/optimize` 新 config（前端拼装）走 `_normalize_config`：`api_url=ai_endpoint`（用户可控）→ SSRF 面与既有能力对齐即可；确认无 Key/异常细节泄露回归
- llm_client `LLMServiceError` 不携带上游正文——确认 `e70b3ff` 之后的重试/退避循环仍无泄露

### E. 引擎修复质量
- DIALOGUE_COUNT REPAIR：与语言/时间轴修复的执行顺序交互（语言修复改写后控制行是否保留）；`lastCompleteResult` 保留语义
- `normalizeFieldInline` 内联拆分的误伤面（正文里出现字段名样式的文字？）；`normalizeNegatedSoundFields` 对合法配乐描述的误杀率；否定列表窗口（前 14 字符）的漏判/误判

### F. 测试有效性
- panel-ai-section 259 断言的静态 includes 是否仍能锁定新交互；panel-generation 行为测试对"迁移 v2 归一""润色沿用主配置"的覆盖是否充分（列出 3–5 个最高价值补充）

## 验证命令（审查中可执行）

```
node --check web/js/minimax_h3_integration.js
node tests/panel-ai-section.cjs      # 期望 PASS: 259 assertions
node tests/panel-generation.cjs      # 期望 PASS
node tests/panel-layout.cjs          # 期望 7 passed
node tests/language-globalization.cjs
python -m pytest tests -o addopts='' -q   # 期望 38 passed, 53 subtests passed
```

本地联调（如需）：ComfyUI 在 `D:\WorkStation\ProjectsVSCode\MiniMaxTool\ComfyUI`（junction 同步仓库），`python main.py --cpu --port 8199`；**改 rules.js 等引擎文件必须重启 ComfyUI 才生效**。

## 输出要求

写《第二轮上线前审查报告》，保存为 `docs/上线前审查报告-2026-09-26-第二轮-codex.md`：

1. **结论**：可上线 / 修复后可上线 / 阻塞（一句话理由）
2. **问题清单**：严重/中等/轻微三级；每条含 文件:行号、描述、影响场景、修复建议；每条必须引用实际读到的代码，禁止无证据猜测
3. **核查项结果表**：清单 A–F 逐项"通过 / 有问题（引用编号）"
4. **测试补充建议**：最多 5 条
5. **通过项**：明确列出已验证无问题部分，避免下轮重复

注意：上一轮 S1/S2/M1–M6/L1/L2 的修复不要重报；但**这些修复在新交互下的回归**属于本轮范围。仅审 `cb4af0c..HEAD` 引入或触及的代码及其与既有代码的交互。
