# lynch — Agent 协作契约

长线标的的**估值框架沉淀**仓库。标的由用户指定；挖掘在 `../invest`，本仓不负责。

面向用户的说明用**简体中文**。代码标识符、`formula_id`、林奇枚举值用英文。

## 真源

| 资产 | 路径 |
|------|------|
| 单票估值框架 | `frameworks/<TICKER>.md` |
| 标的池 | 扫 `frameworks/*.md`（排除 `_TEMPLATE.md` 及 `_` 前缀） |
| 模板 | `frameworks/_TEMPLATE.md` |
| 共享情景轴 | `frameworks/axes/<轴名>.md`（逻辑 id 仍写 `_AXIS.<轴名>`；不进标的池） |
| 演算（后置） | `eval/<TICKER>.py`、`eval/lib/` |
| 跨票口径体检 | `scripts/calibration.py` |

**Ticker 文件名**：美股 **MUST** 带 `.US`（`INTC.US`、`WOLF.US`）；港股/A 股保留市场后缀（`0700.HK`、`601899.SH`）。文件名 = 题头 `# TICKER` = `eval/<TICKER>.py`；与 Longbridge 等多市场符号对齐。细则见 skill `sediment-framework`。

最终真源是仓库里的 Markdown，但沉淀过程 **MUST** 先多轮对话对齐；**MUST NOT** 在用户明确说「可以落盘 / 写成文件 / 确认写入」之前创建或覆盖 `frameworks/*.md`。  
沉淀或更新时 **MUST** 使用 skill **`sediment-framework`**（`/sediment-framework` / `/lynch-framework`）。  
**MUST NOT** 维护 `universe.yaml`（已废止；池子与主尺索引均在框架文件内）。

## 林奇分类

- `lynch_primary` 必填；`lynch_secondary` 可选。
- 取值仅限：`slow_grower` | `stalwart` | `fast_grower` | `cyclical` | `turnaround` | `asset_play`
- **MUST NOT** 把专属叙事（代工重估、净现金修正 PE 等）写成 secondary；那些只写在框架 §1 / Assumptions。

## 框架文档

- 题头（`#` 下两行引用块）：第一行林奇类 / 更新日期 / eval 路径；**第二行 `主尺：…` 一句话索引**（人扫一眼 / 日后脚本抽题头用；细节仍在 §0 / §3）
- 固定章节：§0 现行结论 → §1 专属属性 → §2 Assumptions → §3 可演算块 → §4 Changelog
- 时间线：单文件内嵌；Changelog **append-only** 且**仅用于事件追踪**；**MUST NOT** 用多版本文件名管理
- **Changelog 何时写**：
  - **初建日不写**（首次落盘只改题头 + §0–§3；§4 可留空标题）
  - **框架纠正不写**：口径/公式/证据链/skill 对齐等对框架本身的修订，只改现行块，**MUST NOT** 把纠正过程记进 §4
  - **事件追踪才写**：财报后假设状态变化、里程碑触发等；以**自然日**为单位，同一天多次修订合并进同一节 `### YYYY-MM-DD`，不按小时/不拆多节
- 事件更新：**MUST** 增量修改，**MUST NOT** 无确认整页推翻
- 假设状态：`active` | `confirmed` | `weakened` | `falsified`（证伪改状态，不删行）

## 价带约定（§3 / 日后 eval）— 五档

每票框架算出三个锚点 **\(L < M < H\)**（如何得到由该票主尺决定；例：不对称 SOTP）。

用相邻中点划分五个 **zone**（英文 id 稳定，中文名给人看）：

| zone id | 中文 | 区间 |
|---------|------|------|
| `deep_discount` | 深度折价 | \(P < L\) |
| `conservative` | 保守 | \([L,\ (L+M)/2)\) |
| `neutral` | 中性 | \([(L+M)/2,\ (M+H)/2)\) |
| `optimistic` | 乐观 | \([(M+H)/2,\ H]\) |
| `stretched` | 叙事溢价 | \(P > H\) |

语义：

- **深度折价**：低于保守可算地板（相对本框架）
- **保守 / 中性 / 乐观**：仍在框架内；乐观 ≤「已建模的成功/上沿情景」
- **叙事溢价**：超过框架内成功上沿；仍可能用 stretch 叙事解释，但是 **H 以外**。名字是历史沿用——残差的来源 **MUST** 由该票 §0 的 A2f 记录具名（叙事 / 街倍数 / 费用结构 / 折现率差 / 分支未定），读日报时看来源，不望文生义（实测：`513530.SH` 的残差是 QDII 费 6%，`0883.HK` 的是市价隐含 r≈14%）

### 估值构造口径（真源已移至 skill）

锚点语义（L/M/H 各定价什么）、价带偏离反思的语义门槛、永续增长口径（r 由 7 个篮子按生意性质给出——值的真源 `eval/lib/gordon.py` `BASKET_R`；g 上限 `min(6%, r−4%)`；Gordon、增长的表达顺序、g2 来源优先序、构造规则冲突的裁决顺序、两段式过渡、谷底档 / regime-point 双口径）、L 是情景不是地板、可比性的构造要求、\(V_{opt}\) 与候选带的判据、共享轴的构造要求——**真源在 `.claude/skills/sediment-framework/references/valuation-rules.md`**，本文与标的文档里出现的「AGENTS.md「永续增长口径」」等旧指针均指向该文件。这些只在写 / 纠正框架时需要；日报与 eval 库只依赖本节下面的契约。

**跨票可比性**：`scripts/daily_bands.py` 把所有票的 zone 并排推送，故构造方法 **MUST** 可比。每票 §3 **MUST** 报 \(L/M\)、\(H/M\)、\((H-L)/2M\)（`neutral` 档宽占 M 的比例）；三者的仓库分布由 `scripts/calibration.py` 输出。`_INDEX.md` 每票带篮子列（各腿 `篮 N r%`），由 Inputs `r*` 行的 `篮 N` 标签生成。

**篮子声明 — 行契约**：Inputs 表每条 `r*` 行（`r_disc` 年漂移折现除外）的来源单元格 **MUST** 含 `篮 N`（N ∈ 1..7）+ 归篮理由；eval 以 `inputs.parse_baskets` + `gordon.check_r(r, where, basket)` 校验值与篮子一致，`check_g_cap(g, r, where)` 校验永续 g。

**BLOCKED — 题头契约**：规则拦下但用户拍板入池的票，题头第三行 `> **状态：BLOCKED**（原因；解封条件）`；Outputs 仍写底牌行（散文称对照带）。`daily_bands.py` / `_INDEX.md` / `calibration.py` 对 BLOCKED 票 **MUST NOT** 出 zone、**MUST NOT** 触发跨带告警。

**未建模期权（\(V_{opt}\)）— 行契约**：Outputs bullet `\(V_{opt}\)` + `\(p_L/p_M/p_H\)`；`daily_bands.py` 据此报第二条 `+V` zone。底牌 \(L/M/H\) 与 zone 判定 **MUST NOT** 含概率。判据与构造见 skill references「未建模期权」。

**共识升级候选带（candidate）— 行契约**：Outputs 一行 `- 候选带（标签）：L′ = **x** · M′ = **y** · H′ = **z** · 门槛 Axx`（撇号使其不与底牌正则相撞；无过渡的档原样复制底牌值）；`daily_bands.py` 以第三行 `候选(标签)` 显示并给独立 zone，跨带告警与 `+V` 同权；**MUST NOT** 进底牌 zone、四个跨票比值或 `calibration.py`。何时用、怎么造见 skill references「共识升级候选带」。

**共享情景轴**：多票落在同一条宏观或产业驱动因子上时（金铜价格梯、AI capex 路径、利率路径…），**MUST** 共用同一条情景梯，由各自 `eval/` 跨文件逐格校验，单改一边即红；各票只保留自己对该梯的**敏感度**，**MUST NOT** 各造各的下行。

轴落盘为 `frameworks/axes/<轴名>.md`（不进标的池）；文档与 Assumptions 里的稳定引用写逻辑 id `` `_AXIS.<轴名>` ``，路径由 `eval/lib/axes.py` 的 `axis_path()` 解析。内含三档值、每档具名来源与 as-of、传导规则、跨票加总校验。怎么造见 skill references「共享情景轴」。

价带与 zone 由脚本演算；弱 LLM 至多做归因。**MUST NOT** 让弱流水线擅自改 `frameworks/*.md` 或自行改概率锚点。  
标的文档 **MUST** 输出锚点 \(L/M/H\)、切点 \(A/B\)、以及**填入本票数字的五档价格区间表**与现行 `zone` id。  
**MUST NOT** 在标的文档重复规则长文；契约真源在本节，构造口径真源在 skill references，标的文档只落数字区间。

日报：`scripts/daily_bands.py` 只读框架锚点 + Longbridge 现价 → 写 `output/bands/` → 飞书（**每天都发**；跨带 `@所有人`）。**MUST NOT** 回写框架 MD。

## 密钥与本地

- **MUST NOT** 提交 `.mcp.json`、`config/mcporter.json`、`_local/`
- Wisburg / 行情密钥只放在 gitignore 的本地配置里

## 不要做的事

- 不要在本仓做候选池挖掘或仓位执行建议冒充框架
- 不要把 invest 的 Theme/View YAML 当作本仓估值真源
- 不要编造无来源的财务数字
