# MiniMaxH3-RH-Node · RunningHub 发布手册

> 产物：`dist/MiniMaxH3-RH-Node.zip`（约 250 KB，zip 内含 `MiniMaxH3-RH-Node/` 包文件夹）
> 校验：`dist/MiniMaxH3-RH-Node.zip.sha256`
> 构建：`python scripts/package_runninghub.py`（白名单打包；prompt_engine 以仓库内容为真源，
> 打包不重建并校验 zip 内产物与仓库逐字节一致；不含原 HTML 与任何凭据）

---

## 实测情况

本仓库**未在 RunningHub 平台实测**。参考 GHX 定制版 2026-09-21 的实测结论：RunningHub 共享池
未开放用户侧自定义节点安装（registry 搜索与安装后端均被平台禁用），zip 与 GitHub URL 两种安装
方式都不可用，需走平台审核收录。包内仅注册一个节点 `MiniMaxH3IntegrationRH`（All-in-One），
与官方/GH/GHX 版本互不冲突、可共存。

## 路线一：提交 RH 审核收录

1. 运行 `python scripts/package_runninghub.py` 生成 `dist/MiniMaxH3-RH-Node.zip`。
2. 核对下方「发布前核对清单」。
3. 连同 zip 与 README.md 提交 RunningHub 客服/审核渠道，说明依赖
   （`quickjs-ng==0.16.2.1`、`requests>=2.31`，以及视频链路所需依赖，见 requirements.txt）。
4. 审核通过后 RH 平台侧预装，工作流内直接使用 RH 节点。

## 路线二：私有算力 / 自建 ComfyUI

把 zip 解压到 `ComfyUI/custom_nodes/`，用 **ComfyUI 自己的 Python** 执行：

```sh
python -m pip install -r ComfyUI/custom_nodes/MiniMaxH3-RH-Node/requirements.txt
```

重启后安装完成。若目标环境可以校验注册与示例连线（无需模型权重）：

```sh
python scripts/check_comfy_registration.py /path/to/ComfyUI
```

预期输出 1 个节点 ID（`MiniMaxH3IntegrationRH`）、全部五模式示例的端口类型校验 PASS。

## 安装后的工作流搭建

1. 导入 `examples/<mode>.workflow.json`（五模式任选，见 README 工作流表）；每张图仅 4 节点：
   UNETLoader → `MiniMaxH3IntegrationRH` → CreateVideo → SaveVideo。
2. UNETLoader 选择实际 H3 扩散模型（替换 `SELECT_YOUR_H3_DIFFUSION_MODEL.safetensors` 占位），
   接入节点 `model` 输入。
3. 节点上选择已安装的 H3 文本编码器、视频 VAE、音频 VAE；按模式上传首帧/尾帧/参考素材
   （替换对应 `SELECT_YOUR_*` 占位）。
4. 提示词写进节点的**编辑器**（画布示例已填好各模式的完整提示词）；面板固定以编辑器内容为准。
   仅在 API 调用或直接编辑工作流 JSON 时才用到 `prompt_source` 的 `ai` / `offline` / `format`
   （见 `examples/*.api.json`），`ai` 时在 `ai_*` 参数组填写创意输入与 LLM 配置。
5. 节点内部完成条件构建 → 双时钟采样 → 音视频解码；`frames`/`audio` → CreateVideo → SaveVideo。

## API 调用注意（工作流 API 用户）

- 提交后 `promptTips` 出现 `node_errors` 时，先确认节点 ID 为 `MiniMaxH3IntegrationRH`，
  不要残留旧 GH/GHX/双节点时代的 ID。
- 节点 `ai_api_key` 字段会随工作流保存：**API 提交前清空该字段**，改用对应服务商的环境变量
  （`DEEPSEEK_API_KEY` / `GLM_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY` /
  `DASHSCOPE_API_KEY` / `SILICONFLOW_API_KEY` / 通用 `H3_LLM_API_KEY`）。
- `ai_strict_validation` 默认开启：提示词校验失败节点抛错中止执行；需要拿到文本人工修正时置 false。
- 面板界面只写 `prompt_source=panel`（以编辑器内容为准）；`ai`/`offline`/`format` 三种来源供 API 调度使用，
  此时 `no_subtitle`/`soundscape`/`music` 开关由 `ai_*` 参数组接替。
- 提示词优化器路由为 `/rh/minimax-h3/prompt-optimizer/...`，前端自动匹配，无需配置。

## 本机实测（2026-09-24，ComfyUI 0.37.0 / Python 3.10 / CPU 模式）

- **注册**：`check_comfy_registration.py` 通过；节点以 `MiniMaxH3IntegrationRH - MiniMax H3 All-in-One (RH)`
  注册，五模式示例端口类型全部 PASS。
- **服务加载**：真实 `python main.py --cpu` 启动，`MiniMaxH3-RH-Node` 导入 0.8 秒无错误；
  `/object_info` 确认 62 输入（36 必填 + 26 可选）与 4 输出（frames/audio/video_latent/report），
  旧 6 节点 ID 已消失，与 GHX 6 节点共存无冲突。
- **图校验**：4 节点示例图提交 `/prompt`，62 个输入全部结构校验通过；仅有的 node_errors
  是 4 个模型文件 combo 值不在列表（本机未安装 H3 权重），符合预期。
- **execute 行为实测**（`test_allinone_execute.py`，在 ComfyUI 运行时上下文直调节点）：
  ai 占位 Key 报明确错误、ai 无 Key 报“请设置服务端 …”、时长 >15s 被守卫拦截、
  offline/format 均通过 quickjs 引擎与严格校验后失败在模型加载处（无权重环境的最深可达点）。
- **端到端视频生成**：待安装 H3 模型权重后验证（见下方未勾选项）。

## 发布前核对清单

- [x] 节点 ID `MiniMaxH3IntegrationRH`（单节点注册），与官方/GH/GHX 版本不冲突
  （`check_comfy_registration.py` 断言注册数 1）
- [x] 包名/UA/类名无原作者字样；LICENSE 与 NOTICE 署名保留（GPL-3.0-or-later 合规）
- [x] 白名单打包：zip 不含原 HTML、内置凭据（`_decodeKey` 已替换为 server-managed）、`__pycache__`
- [x] zip 含包文件夹结构（`MiniMaxH3-RH-Node/`），解压即得正确层级
- [x] requirements.txt 齐全（quickjs-ng / requests / 视频链路依赖）
- [x] 五模式示例经引擎严格校验 valid；`check_comfy_registration.py` 可离线校验端口类型
- [x] prompt_engine 以仓库内容为真源；打包不重建，并校验 zip 内产物与仓库逐字节一致
- [x] 本地 ComfyUI 实测：注册、服务加载、图校验、execute 提示词路径全通过（2026-09-24，CPU）
- [ ] 安装 H3 模型权重后端到端生成一次视频
- [ ] RH 审核收录通过 + 平台侧真实运行一次
