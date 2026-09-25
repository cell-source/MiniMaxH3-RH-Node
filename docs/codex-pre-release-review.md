# 给 Codex 的任务提示词：上线前代码与功能审查

> 使用方式：把本文件全文作为任务提示词投喂给 Codex，让它在仓库根目录（d:\WorkStation\ProjectsVSCode\MiniMaxTool\MiniMaxH3-RH-Node）下工作。
> Codex 只做审查与报告，**不要修改任何代码**。

---

## 角色

你是资深 ComfyUI 插件与全栈代码审查员，熟悉 JavaScript ESM / aiohttp 服务端 / Python 并发 / 工作流序列化兼容问题。任务是在功能上线（RunningHub 平台发布）前，对最近一轮 UI 重构做严格的代码审查 + 功能审查，找出真实缺陷并给出可执行的修复建议。

## 项目背景

- 本仓库是 ComfyUI 自定义节点包（RunningHub 平台发布）：单节点 `MiniMaxH3IntegrationRH`（`video_nodes.py`），面板 UI 为 `web/js/minimax_h3_integration.js`（3748 行单文件 ESM，CSS 内嵌模板字符串）。
- 提示词引擎：`prompt_engine/`（quickjs 沙箱，`entry.js` 三入口：operation=`ai` 在线生成 / `format` 离线整理 / 其余离线包装）；Python 侧出口在 `prompt_runtime.py` + `llm_client.py`（7 家服务商预设）。
- 服务端路由在 `prompt_optimizer.py`（1736 行）：`/rh/minimax-h3/prompt-optimizer/*`（config/models/optimize/start/status/cancel + 本轮新增 `generate`、`format`）。
- 面板隐藏 widget（`prompt_source`、`ai_text`、`ai_provider`、`ai_api_key`、`ai_enrich` 等约 19 个）不显示在 UI 但保留序列化，供工作流运行期兼容（`video_nodes.py` execute 的 `prompt_source != "panel"` 分支）。

## 审查范围（git 范围 `67e0cb8..HEAD`，共 6 个提交）

本轮把原「AI 生成」面板区块彻底重构为"统一配置界面 + 编辑器填回"模式：

1. `6b05d5c` 批次A：删除面板 AI 生成区块（details.mxv-ai 及全部 DOM/变量引用），ai_* widget 保留隐藏序列化；`syncAiControls` 改为空实现桩
2. `cb3dfe2` 批次B：新增 ✨AI 生成按钮与生成弹窗（8 个开关直写隐藏 ai_* widget + 生成语言 + AI 超时）、「H3 格式整理（离线）」按钮；后端新增 `/generate`、`/format` 两个路由（`prompt_optimizer.py` 的 `generate_prompt_api` / `format_prompt_api` / `_run_generation_operation` / `_generation_payload_controls`）；新增 `tests/test_generation_api.py`
3. `17dda81` 生成弹窗语言/超时平铺（撤折叠）
4. `dfdbdc5` ✨ 与 ⚙ 合并为统一配置界面 `openOptimizerSettings`：内嵌「AI 生成选项」区块；生成动作（生成并填入 / H3 格式整理）与保存并列；生成前 `commitGenerationConfig()` 落盘
5. `f11dea3` 文案澄清（✦润色 vs ✨AI 生成职责说明）
6. `41ee9d2` 润色收编：删除工具条 ✦ 按钮；润色 = 配置项「运行前自动润色」勾选 + 配置界面底部「✦ 润色」按钮（`applyDialogConfig()` 先落盘再 `runPromptOptimization()`，与保存共用逻辑）

改动统计：`web/js/minimax_h3_integration.js`（±371 行）、`prompt_optimizer.py`（+80）、`tests/panel-ai-section.cjs`（±108）、`tests/test_generation_api.py`（新增 176）。

## 必读文件（按优先级）

1. `web/js/minimax_h3_integration.js` — 重点：`openOptimizerSettings`（统一配置界面全文，约 2085–2530 行）、`runPromptOptimization`/`applyDialogConfig`/`polishButton.onclick`/`runGeneration`/`generationBody`/`applyGenerationResult`（约 2380–2530、2700–2770 行）、`syncAiControls` 桩、隐藏 widget 恢复回路（`onConfigure` 内）、`promptTools.append(...)` 工具条
2. `prompt_optimizer.py` — 重点：`_H3_GENERATION_CONTROLS`、`_generation_payload_controls`、`_run_generation_operation`、`generate_prompt_api`、`format_prompt_api`、`register_prompt_optimizer_routes` 尾部两行注册
3. `video_nodes.py` — `execute` 的 `prompt_source != "panel"` 分支（约 710–760 行）：确认面板新路径写入的 widget 值与该分支消费的语义一致（尤其 `ai_mode`、`ai_language`、`ai_enrich`、`ai_strict_validation`、`ai_api_key`）
4. `prompt_runtime.py` + `prompt_engine/entry.js` — 引擎 controls 契约（`elements[id]` 必须存在，缺 key 会抛 `Unknown control`）
5. `llm_client.py` — `make_client` 的 provider 预设与 Key 环境变量回退
6. `tests/panel-ai-section.cjs`（278 断言）、`tests/test_generation_api.py`（12 用例）、`tests/panel-layout.cjs`、`tests/language-globalization.cjs`、`tests/test_prompt_node.py`

## 重点审查清单

### A. 正确性与兼容（上线阻塞级优先）
- 面板开关直写隐藏 widget（`ai_enrich`/`ai_soundscape`/…/`ai_strict_validation`、`ai_provider`/`ai_model`/`ai_endpoint`/`ai_api_key`/`ai_language`/`ai_timeout`）：逐一核对 `video_nodes.execute` 与 `prompt_engine/rules.js` 消费语义是否一致（名称、布尔/字符串类型、默认值）。特别验证：面板生成的请求体（`generationBody()`）与运行期 `execute` 的 ai 分支（`controls` 字典构造）对同一组 widget 值是否产生等价行为。
- `commitGenerationConfig()` / `applyDialogConfig()`：生成/润色前落盘的时序是否有遗漏（例如 keyOwner 归属写回 vs `provider.onchange` 的回存逻辑是否双重写入或串平台）。
- 旧工作流兼容：`prompt_source` 为 `ai`/`offline`/`format` 的旧序列化值；`(none)` 快照（`_coerce_int` 等）；`gh_state_json` 恢复路径；`syncAiControls()` 空桩在 `onConfigure` 的调用安全。
- undo 语义：`applyGenerationResult` 中 `pushPromptUndo(promptSnapshot())` 先于 `applyOptimizedPrompt`；验证 Ctrl+Z 能回到生成前文本、且 `promptByMode`/`setPromptWidget` 状态一致。

### B. 并发 / 竞态 / 生命周期
- `aiGenerating` 与 `optimizing` 两个互斥标志：弹窗内同时点「✦ 润色」「生成并填入」「H3 格式整理」、润色进行中关弹窗、生成进行中点节点删除（`onRemoved` 清理是否覆盖新路径）、`updateWorkflowState` 运行中取消逻辑是否仍正确。
- `runGeneration` 的 `finally` 在 `overlay.remove()` 之后仍引用 `runButton`/`save` 等 DOM 引用（闭包持有、无 null 解引用？），以及失败时弹窗保留、成功时关闭的 UX 一致性。
- 后端 `asyncio.to_thread(_run_generation_operation, ...)`：与 `/optimize` 的 `_ACTIVE_REQUESTS` 注册表不同——新端点无 request_id/取消机制，评估 RunningHub 托管环境下长请求（最长 600s 超时）是否可接受、事件循环是否被阻塞（`generate_prompt` 内 quickjs + requests 流式均在线程中执行？逐行确认）。

### C. 安全
- `/generate`、`/format` 入参：`_generation_payload_controls` 白名单是否完备（`prompt` 文本长度上限？`provider`/`endpoint`/`model` 直接传入 `make_client`，SSRF 面 = 用户提供 endpoint → 服务端发请求——与 `ai_endpoint` widget 的运行期行为对齐即可，但请确认无新增暴露）。
- `api_key` 在响应/日志/序列化中的泄露路径（对照旧代码 M12 修复：保存工作流前剥 Key 的约定是否被新 UI 打破——`sanitizeOptimizerForWorkflow` 是否仍覆盖新写入的 `optimizerSettings`）。
- 新端点错误响应 `{"error": str(error)}` 是否会把内部异常细节（含 Key 片段）带出。

### D. UI/UX 功能审查
- 统一配置界面：8 个开关读取时机（打开弹窗时从 widget 读）与写回（onchange 即写 widget）；「保存」与「✦ 润色」「生成并填入」对同一状态的写序冲突。
- 高级选项区块：`no_subtitle`/`soundscape`/`music` 三行删除后 `updateAdvancedVisibility` 的可见清单、恢复回路只回读 `strict_prompt_tags`——确认无残留引用、无 widget 值与 UI 脱节。
- i18n：新增键（`Polish`/`AI generation options`/`Generation language`/`AI timeout` 等）在 `DOM_TRANSLATIONS` 的覆盖；`t()` 未命中时英文回退是否可接受。
- 弹窗滚动保护 `max-height + overflow-y` 在小屏的表现；弹窗无 Escape 关闭、生成中禁止关闭（`close` 有 `aiGenerating` 守卫）是否合理。

### E. 测试有效性
- `tests/panel-ai-section.cjs` 大量 `js.includes(...)` 静态断言：指出哪些断言会在重构时静默失效、哪些关键行为缺测试（建议列出 3–5 个最高价值的新增测试）。
- `tests/test_generation_api.py`：stub `folder_paths`/`server` + 合成包 `h3pkg` 的加载方式是否有隐患；用例是否覆盖了 `enrich_*` 开关传导到引擎。

## 验证命令（审查中可执行）

```
node --check web/js/minimax_h3_integration.js
node tests/panel-ai-section.cjs      # 期望 PASS: 278 assertions
node tests/panel-layout.cjs          # 期望 7 passed
node tests/language-globalization.cjs
python -m pytest tests -q            # 期望 28 passed
```

本地联调环境（如需起服务）：ComfyUI 在 `D:\WorkStation\ProjectsVSCode\MiniMaxTool\ComfyUI`（junction 同步本仓库），`python main.py --cpu --port 8199`；端点冒烟：`POST /rh/minimax-h3/prompt-optimizer/format`。

## 输出要求

写一份《上线前审查报告》，保存为 `docs/上线前审查报告-2026-09-25-codex.md`，结构：

1. **结论**：可上线 / 修复后可上线 / 阻塞（一句话理由）
2. **问题清单**：按 严重（数据损坏/安全/上线阻塞）/ 中等（功能缺陷/边界）/ 轻微（体验/代码质量）三级；每条含：文件:行号、问题描述、影响场景、修复建议（具体到改法）。禁止无证据的猜测——每条必须引用你实际读到的代码
3. **重点核查项结果表**：上面对照清单 A–E 逐项给"通过 / 有问题（引用问题编号）"
4. **测试补充建议**：最多 5 条，按价值排序
5. **通过项**：明确列出验证过且无问题的部分，避免下轮重复审查

注意：本仓库近期刚完成一轮审查修复（46caf61…67e0cb8，见 `docs/代码审查报告-2026-09-25.md`），历史问题不要重复上报；只审 `67e0cb8..HEAD` 引入或触及的代码，但需要验证它们与既有代码的交互。
