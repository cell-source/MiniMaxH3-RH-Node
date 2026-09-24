# 任务：修复 MiniMaxH3-RH-Node 面板底部被裁剪/超出问题（Copilot 三轮未解决，交接）

## 你接手时的状态

仓库：`d:\WorkStation\ProjectsVSCode\MiniMaxTool\MiniMaxH3-RH-Node`（HEAD = a8e241b，工作树干净）
目标文件：`web/js/minimax_h3_integration.js`（单文件 ESM 前端，CSS 内嵌在模板字符串中，sha256 前 16 位 `6c8166e66cfc6302`）

用户问题（连续 4 轮反馈未闭环）：**面板底部内容（高级选项卡片下边缘/最后一行"配乐"）看起来超出主界面或被裁掉**。用户截图红框圈的是"配乐"开关行及其下方区域。

## 已确认的布局机制（当前代码）

- 面板是 DOM widget（widget 名 `mxv_panel`），`createPanel()` 内 `syncLayout()` 控制节点高度。
- 当前公式：`nodeH = domWidget.y(254，标题栏+原生widget行占位) + root内容逐项求和 + BOTTOM_DECOR_HEIGHT(20)`。
- 内容测量 `measureContentHeight()`：逐子元素 `offsetHeight + margin` 求和 + flex gap 6px。
- 已做过的修复（全部已提交，不要回退）：
  1. `2fd7243` 手风琴 flex-shrink:0（防压扁）
  2. `8f614cf` 完全展开布局：节点高度自适应内容、无滚动条；编辑器从 absolute 改文档流
  3. `2aeefcf` 测量改逐项求和 + margin；CSS 冗余 margin 清理；AI 凭据收纳成摘要行
  4. `fc1a31c` 行 min-height 30→26、body 底 padding 8→4
  5. `8220b4b` details 收起规则 `:not([open])>.mxv-advanced-body{display:none!important}`（body 内联 display:grid 会覆盖 UA 隐藏规则）
  6. `a8e241b` nodeH 加 `BOTTOM_DECOR_HEIGHT=20`（补偿 ComfyUI 容器渲染高度差）

## 实测数据（Playwright，浏览器 127.0.0.1:8199 已运行）

当前状态几何全部"正常"：
- `nodeSize=[500,1159]`，`widgetY=254`，`computedHeight=905`
- `rootOffsetH=885 == rootStyleH=885 == contOffsetH=885`（容器精确包住内容）
- `contVsRoot=0`，`overflow(root.scrollHeight-offsetHeight)=1`（舍入）
- 公式校验：`nodeH(1159) - widgetY(254) - 20 = 885 == rootStyleH` ✓
- 各子元素 offsetHeight：size 24 / modes 34 / box 186 / prompt-wrap 299 / ai 手风琴 74 / 高级手风琴 234

**几何上一切都对，但用户仍然说"还是不对"。** 这是交接的核心难点：静态几何测量无法复现用户看到的问题。

## 用户截图特征（唯一的复现线索）

用户两次截图（间隔一轮修复）内容几乎相同：
- 红框圈住**最后一行"配乐"开关 + 其下方的区域**
- "音景"开关行之后、"配乐"行下方有一条**颜色比卡片背景浅/深不一的横带**，看起来像卡片边界错位或两层底色叠加
- 注意第二次截图：高级选项卡片从"音频模式"到"配乐"共 6 行，但卡片底边框似乎在"配乐"行上方就画完了，"配乐"行悬在卡片外面
- **两次截图的节点高度看起来都远小于内容需求**（截图里能看到配乐行已在面板最底部边缘）

## 需要你排查的方向（按可能性排序）

1. **canvas transform 缩放下的视觉误差**：用户浏览器 zoom/scale 与测试时不同（我测试时 scale=0.66~0.7）。`getBoundingClientRect` 读数受 scale 影响，ComfyUI 的 dom-widget 定位可能有亚像素累积误差，在特定 scale 下"配乐"行与卡片底边错位 1-2px 视觉可辨。→ 排查：在不同 `app.canvas.setZoom(0.4/0.6/0.8/1.0)` 下测量 `details 卡片底边` 与 `最后一行底边` 的未缩放差值（用 offsetTop 链而不是 rect）。

2. **ComfyUI dom-widget 容器的高度分配时机**：`computedHeight=905` vs `nodeH-widgetY-20=885` —— ComfyUI 自己认为 widget 高 905，但我们给容器的 root 是 885。差 20px 恰好等于 BOTTOM_DECOR_HEIGHT。**可能 ComfyUI 的 computedHeight 已经包含了它自己的 decor 扣减，我们的 +20 反而多补了**（即双重补偿）。→ 排查：读 ComfyUI 源码 `web/lib/litegraph/core/src/LGraphCanvas.ts` 或 `widgets` 相关代码，找 dom-widget 容器高度分配逻辑（搜 `dom-widget`、`computedHeight`、`size-full` class），确认 `(nodeH - widgetY)` 与容器实际高度的关系到底是"少 20"还是"相等"——我此前用 offsetHeight 对比得出"少 20"，但容器可能自身有 margin/padding 造成读数偏差。若确认双重补偿，删掉 BOTTOM_DECOR_HEIGHT 并改用 `computedHeight` 直接对齐。

3. **节点底部"重绘装饰"**：`drawAspectDisplayOverlay` 在 canvas 上画"自适应"覆盖层；节点本身 `onDrawForeground` 有自定义绘制。若节点 canvas 绘制的底部边框/圆角矩形比 DOM 容器矮，会形成"卡片在 DOM 里完整但视觉上被节点边框切掉"的错觉。→ 排查：临时隐藏 DOM 面板（`root.style.display='none'`）看节点 canvas 底部绘制范围；或直接读 `node.size` 与画布上节点实际绘制矩形的关系。

4. **用户工作流是旧状态恢复**：`mxv_state` 的 `height` 字段存的是历史高度。若恢复时 `savedState.height` 被优先采用而 syncLayout 后续没触发，初始渲染可能短暂错位。→ 排查：`onConfigure`/`onGraphConfigured` 恢复路径里确认 `syncLayout(userHeight, true)` 之后有没有被后续 render 覆盖。

## 环境与工具（务必照做，能少踩 90% 的坑）

- **本机无全局 Node**：先 `$env:Path = "D:\Tools\nodejs;$env:Path"`。
- **本地 ComfyUI 正在运行**：`D:\WorkStation\ProjectsVSCode\MiniMaxTool\ComfyUI`（`python main.py --cpu --port 8199`），`custom_nodes/MiniMaxH3-RH-Node` 是指向本仓库的 junction → **改 JS 后浏览器刷新即生效**。
- 测试浏览器已打开：`http://127.0.0.1:8199/`，画布上有 AIO 节点。若需重载工作流注意 beforeunload 对话框（连续两次 reload 或 `page.goto` 同 URL 可过）。
- **节点滚出视口时 ComfyUI 会给 dom-widget 容器 `display:none`，所有测量变 0** → 测量前把节点挪回视野：`aio.pos=[300,100]; app.canvas.ds.offset=[-(300*scale)+120, -(100*scale)+80]`。
- **rect 读数受 canvas scale 影响**（我因此误判过一次"差 20px"）：对比高度用 `offsetHeight/clientHeight`（不受 transform 影响），不要用 `getBoundingClientRect().height`。
- **JS 是 ESM**：语法检查拷成 `.mjs` 再 `node --check`。
- **PowerShell 5.1 内嵌 python -c 处理含引号/反引号/中文的代码必炸** → 改文件一律用独立 Python 脚本（utf-8 读写 + 精确串 replace + count 断言），参考 `D:\Tools\h3_llm\fix_*.py` 的模式。含中文的 `.ps1` 必须 UTF-8 with BOM。
- Playwright 调用返回 `deferredResultId` 时，用同 pageId + 空 code 再调一次继续等待。

## 回归命令（每次修改后全跑）

```powershell
$env:Path = "D:\Tools\nodejs;$env:Path"
node tests/panel-ai-section.cjs        # 277 断言
node tests/language-globalization.cjs  # 89 断言
python -m pytest tests -q              # 16 断言
node --check (拷贝为 .mjs)             # 语法
```

## 验收标准

1. 用户在**任意画布缩放级别**下，高级选项卡片底边框完整包住"配乐"行，无错位、无底色断层、无内容被裁。
2. 展开/收起两个手风琴、切换 AI 来源（panel/ai/offline/format）、切换主模式（首尾帧/全能参考），节点高度实时自适应且**能回落**，全程无滚动条、无裁剪。
3. 所有回归通过 + 浏览器截图给用户人工确认。

## 沟通建议

用户已连续 4 轮看到"修好了但还是不对"，信任度在下降。修完后**务必自己先在不同 zoom 级别截图自查**，再请用户确认；如果静态几何全部正常但用户仍说有问题，优先怀疑"视觉渲染层"（canvas 绘制、scale 亚像素、ComfyUI 容器分配）而不是继续调 CSS 数值。
