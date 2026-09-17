# H3 Fight Scene — 打斗 Skill 落地差异清单

> 状态：调研与适配分析完成，工具代码未做任何改动。
> 本文档回答三个问题：社区资产有什么可用、与本工具有什么冲突、落地路径是什么。

---

## 一、素材来源与许可

下载位置：`skills/h3-fight-scene/references/community/`（2026-09-17）

| 本地文件 | 来源 | 许可 | 备注 |
|---|---|---|---|
| `jojocodex-wushu-prompt-skill-v3.md` | HuggingFace `Jojocodex/wushu-action-v7-minimax-h3-fl2va-ref2va-lora` → `武术打斗提示词skill.md` | **License: other**（HF 页面标注，非标准协议） | 13k 下载/月，v7 六天前更新，社区事实标准 |
| `jojocodex-move-tags-full-list.md` | 同仓库 `招式TAGS完整清单.md` | 同上 | 924 条训练 caption 全量招式词频 |
| `jojocodex-h3-fight-simulator-guide.md` | 同仓库 `H3武斗模拟器_使用指南.md` | 同上 | 单文件 HTML 模拟器的说明（模拟器 exe 本体未下载） |
| `mujiu186-jingjie-wuxi-skill.md` | GitHub `mujiu186-create/-agent-` → `打斗skill/武戏Skill.md` | 仓库**无 LICENSE** | 镜界·武戏动作指导系统 |
| `mujiu186-martial-arts-reference.md` | 同仓库 `打斗skill/martial-arts-reference.md` | 同上 | 5 阶段动作分解素材库 |

**使用边界**：以上文件仅作为本工具私有参考与改写底稿（本地自用）。若未来将本工具开源或公开分发，必须删除或彻底重写该目录，不能连带再分发上游原文。

---

## 二、三份资产的定位与取舍

| 资产 | 定位 | 取舍 |
|---|---|---|
| **Jojocodex V3 规则体系**（15 章） | **知识主底座**：输出格式就是 H3 官方字段（`integrated_multimodal_description` / `[Shot N]` / `overall_soundscape` / `non_diegetic_music`），含攻防逻辑、分级伤害、连招链、运镜库、声画分层、模板与 3 个成品示例 | 采纳 90%：规则体系 1:1 迁移；剥离 LoRA 专属内容；正文英文化 |
| **H3 武斗模拟器**（说明文档） | **编排生成思路**：先精确结算对局再写成连续动作段；角色卡（9 级武力/8 种战斗风格/四类招式）、5 种情景库、【电影级打斗】规则块、AI 润色硬约束 | 采纳方法论：情景库与规则块设计可移植；模拟器本体（exe/网格结算）与提示词工具无关，不引入 |
| **镜界·武戏**（mujiu186） | **方法论补充**：动作 5 阶段分解（起始→发力→接触→受力→结束）、呼吸帧节奏、协同校验（禁傻站挨打/道具连续性/视线连续性） | 采纳方法论：5 阶段分解与工具"AI 精细时间戳"分段机制天然对应；其输出格式非 H3，不直接采用 |

---

## 三、硬约束冲突（必须适配，否则工具校验不通过）

### 3.1 语言边界 —— 已核实校验器实现

本工具 `validateEnglishDescription`（HTML L2809）实现为：

```js
return !/[\u3400-\u9fff]/.test(stripDialogue(body));
```

**对白 `<d>` 块之外出现任何 CJK 字符（\u3400-\u9fff）直接判 invalid。**

- Jojocodex V3 的规则正文与示例是中文（本地 ComfyUI 路线，Qwen3-VL 文本编码器懂中文）→ **走官方 API 必须全部英文化**
- HF 模型卡的成品模板 T1-T5 也印证了这一点：作者自己写 API 可用的稿子时，散文部分用英文、仅在 LoRA 路线保留中文招式标签（扫堂腿/缠头裹脑）
- **适配产物**：需要一份「招式中英对照词库」（见 §四.6），输出正文一律英文；拼音只作词库内部索引，不进输出

### 3.2 LoRA 专属内容 —— 剥离或降级

| V3 内容 | 性质 | API 路线处置 |
|---|---|---|
| 触发词 `wushu_action,` | LoRA 激活词 | **删除** |
| 招式频次表 + 三级选用优先级（高频/低频/库外借词） | LoRA 响应强度优化 | **降级**：去掉频次语义，保留核心原则「招式宁专勿泛、术语精确」 |
| LoRA 强度 0.7-1.1 调参、seed 纪律、124/362 帧 17n+5 网格 | 本地 ComfyUI 参数 | **移入附录**（标注"仅本地路线适用"） |
| 「生成参数与提示词正文彻底分离」原则 | 通用设计 | **保留**——与本工具"时长/分辨率在 UI 选择、不进正文"的设计完全一致 ✅ |

### 3.3 配乐策略 —— 从固定 None 改为可选

V3/模拟器固定 `non_diegetic_music: None.`（写实格斗只留原生打击音效）。
本工具按「配乐」勾选项生成音乐或 N/A。

- **适配**：打斗模块不硬编码 None；写实档默认建议不勾配乐，允许用户勾选时生成打击乐驱动配乐（percussive action score）；工具的 `optionContract` 已天然支持，无需改校验器。

### 3.4 其他自动满足项（无需改动，记录为设计依据）

- `[Shot 1]` 开头、Shot 1 不带时间戳、切点严格递增且 < 时长 → V3 的三段式分镜结构天然满足
- 打斗场景通常无对白 → 说话人/对白校验基本不触发；若需喊话（「接招！」）按官方 `<d>` 格式处理
- 7000 字符上限 → V3 完整版模板实测在限内

---

## 四、方法论吸收清单（价值排序）

1. **攻防逻辑规则**（V3 §三）：禁回合制交换（"你一下我一下"是最大穿帮）、压制方连出 3-5 招不停手、防守方连续招架流退（格挡/撤步交替、每挡一击退半步）、抓旧力已尽新力未生的空档反击、禁双人同时防守/站桩对峙
2. **分级伤害反馈 Lv1-Lv4**（V3 §四）：肩震→踉跄→破防跪地→超重击飞（凌空滞空→侧滚 2-3 圈→撞墙碎石→瘫软）；**击飞方向必须沿击打作用线**（正面重拳→正后方、扫堂腿→横向翻转、上勾拳→竖直腾空）
3. **时序闭环**（V3 §七 + 镜界武戏 5 阶段）：起势→发力→接触→受力→收势逐阶段描述，收势即下招起势；激烈动作后可插 0.5-1s 呼吸帧（对峙喘息、环境声突显）——与工具"AI 精细时间戳"的分段写法直接对应
4. **运镜库**（V3 §八）：胸口压迫手持跟拍、短弧侧绕、低位擦镜（贴地扫腿视角）、命中瞬间急推近景 0.5 拍、跟随击飞轨迹全程跟拍、撞击时镜头震动；**禁止**空镜/冻帧/慢动作/无理由抖动
5. **声画分层库**（V3 §九）：`overall_soundscape` 按 5 层各选 4-6 项——打击层（拳腿破空、格挡沉闷撞击、重拳轰鸣）、身法层（衣料摩擦、脚步刮擦）、击飞层（人体凌空风声、墙体碎裂）、环境层（碎石飞溅、金属管件坠落）、人物层（急促喘息、发力低喝）→ **可直接英文化为离线场景模板的 ss 字段与 AI 路径音景参考**
6. **招式中英对照词库**（起点，待扩充）：源自 `jojocodex-move-tags-full-list.md` 高频词：

   | 中文 | 英文输出写法 |
   |---|---|
   | 扫堂腿 | a low sweeping kick that scrapes the floor |
   | 鞭腿 | a whipping roundhouse kick |
   | 腾空侧踹 | a flying side kick, heel leading |
   | 后旋踢 | a spinning back kick |
   | 贴山靠/铁山靠 | a shoulder charge that drives the whole body forward |
   | 直拳/崩拳 | a straight punch / a snapping linear punch |
   | 重锤重拳 | a full-weight haymaker punch |
   | 过肩摔 | an over-the-shoulder throw |
   | 接腿摔 | catching the incoming leg and throwing |
   | 擒拿 | a wrist-lock joint maneuver |
   | 格挡卸力 | a deflecting block that dissolves the force sideways |
   | 闪身 | a rapid sidestep |
   | 缠头裹脑 | the blade circling behind the head and back in one flowing arc |
   | 回马枪 | a turning reverse thrust over the shoulder |
   | 撩剑 | an upward rising blade flick |
   | 扎枪 | a linear spear thrust |
   | 横扫 | a horizontal sweeping arc |
   | 弓步/马步 | a bow stance / a rooted horse stance |
   | 腾空/纵跃 | leaping airborne / a springing jump |
   | 翻滚卸力 | a rolling break-fall that dissipates the momentum |

7. **人物锚句**（V3 §十四）：外貌（发色+发型+服装+体型+兵器）只在开头「角色设定」块定义一次，全篇逐字固定，镜头内只称 Character A/B——与 Ref2VA `subject_definitions` 原则同构
8. **避坑清单**（V3 §十五）：一个 Shot 只放 1-2 个招式、招式宁专勿泛（"a low sweeping kick" ✔ "kicks" ✘）、禁写"静止站立/呆立"、禁开场对峙摆姿势
9. **【电影级打斗】规则块**（模拟器）：动势连招（收势即起势）、同招多变（换角度/节奏/轻重，绝不复读）、攻守瞬时反转、快慢相间（重击前轻招铺垫、爆发后留半拍喘息）
10. **情景库**（模拟器）：阵地战/擂台战/追逐战/空战/水战 → 各自带默认场景与建议运镜，可映射为打斗模块的子风格

---

## 五、边界与红线

1. **写实 vs 仙侠分档**：V3 严格写实（禁玄幻光效/剑气/护体屏障）；镜界武戏允许仙侠特效但要求"能量守恒"（特效强度与动作力度匹配）。→ 自有 skill 分两档：**写实档**（默认，同 V3 禁令）与 **仙侠档**（允许能量特效，附守恒约束），用户按输入内容自动分流或手动选择
2. **内容安全**：V3 词库含"血线/见血"表述；官方 API 有内容审核，过激词汇可能拦截或整单失败。→ 词库提供克制版替换（blood line → a thin red scrape；见血 → visible injury feedback），并在模块中默认使用克制版
3. **许可**：上游均为"other/无协议"，自有 SKILL.md 必须是**重写**而非翻译拷贝；规则思想不受版权保护，但表述需自撰

---

## 六、落点到本工具的三层路径（预览，待确认后实施）

| 层 | 内容 | 改动量 |
|---|---|---|
| **A. 离线场景模板** | `SCENE_LIST` 增加 `fight` 条目（keys: 打斗/战斗/武打/格斗/fight/battle/combat…，注意排除"打"单字误报；ss/mu 取自 §四.5 声画分层库英文化） | 小：一个数据条目 |
| **B. AI 路径注入** | `runAiEnrich` 检测打斗输入时向 system prompt 追加打斗专项模块（攻防逻辑+运镜+分级反馈+5 阶段时间戳编排）；需一条定向放行：允许细化既有打斗动作的招式展开（仍禁新增人物/兵器/结果） | 中：一处注入点 + 指令文本 |
| **C. 自有 Skill 文档** | `skills/h3-fight-scene/SKILL.md`（Agent 可读，重写版规则+英文化词库+两档风格） | 中：一篇重写文档 |
| **D. 配套** | 打斗示例按钮、`技术文档.md` 场景模板章节、`使用说明.md` 提一句 | 小 |

建议实施顺序：C → A → B → D（先把知识沉淀为可审查的文档，再动代码）。

---

## 七、待确认问题（用户拍板）

1. **打斗范围**：写实徒手（V3 全覆盖）/ 器械（V3 含剑刀枪）/ 仙侠特效（需自建仙侠档）——第一版先做哪些？
2. **扩写授权**：AI"丰富"模式下允许细化打斗招式的边界（建议：允许细化输入已声明的动作如何起势发力命中收势；禁新增参与者/兵器种类/胜负结果）
3. **触发方式**：关键词自动检测 / 显式 UI 开关 / 两者结合
4. **实施顺序**：按建议 C→A→B→D，还是先做某一层？

---

## 八、本轮已完成 / 未做

- ✅ 下载 5 份社区源文件到 `references/community/`（来源与许可已记录）
- ✅ 核实本工具英文校验器实现（CJK 全拒绝）→ 确认英文化为硬性前提
- ✅ 产出本差异清单
- ❌ 未改任何工具代码（HTML/SCENE_LIST/AI 注入均未动）
- ❌ 未写自有 SKILL.md（待 §七 确认后按 C 实施）
