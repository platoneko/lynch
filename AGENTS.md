# lynch — Agent 协作契约

长线标的的**估值框架沉淀**仓库。标的由用户指定；挖掘在 `../invest`，本仓不负责。

面向用户的说明用**简体中文**。代码标识符、`formula_id`、林奇枚举值用英文。

## 真源

| 资产 | 路径 |
|------|------|
| 单票估值框架 | `frameworks/<TICKER>.md` |
| 标的池 | 扫 `frameworks/*.md`（排除 `_TEMPLATE.md` 及 `_` 前缀） |
| 模板 | `frameworks/_TEMPLATE.md` |
| 共享情景轴 | `frameworks/_AXIS.<轴名>.md`（`_` 前缀，不进标的池） |
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
- **叙事溢价**：超过框架内成功上沿；仍可能用 stretch 叙事解释，但是 **H 以外**

### 锚点语义（L/M/H 各定价什么）

zone 的中文名只有在三档语义一致时才跨票可比。**怎么造**见 skill `sediment-framework` A2e；**是什么**在这里定。

| 锚点 | 定价 |
|------|------|
| **M** | 「当期已知事实长期化」：分子取当期可观测或共识基线，倍数取自身历史中位或同业簇中位；不预设改善也不预设恶化 |
| **L** | 「已**实测过**的坏」：分子与倍数各自退到有实证的下行位置，不是想象的坏。构造按 `lynch_primary` 分档 |
| **H** | 「**已建模**的成功上沿」：分子取共识/指引上沿，倍数取自身历史高分位或同业簇上沿；**MUST NOT** 含多一年的复利或未建模的第二段增长 |

**L MUST 是情景，不是地板。** 资产地板（PB / 净现金 / 清算值）是独立参照，用来说明 `deep_discount` 能跌到哪。地板允许兜底 \(L=\max(\text{scenario},\ \text{floor})\)，但兜底时 **MUST** 同时报出情景值——否则同一个 `conservative` 在两票之间一个读作「便宜」、一个读作「生意不成立」。

**跨票可比性**：`scripts/daily_bands.py` 把所有票的 zone 并排推送，故构造方法 **MUST** 可比。每票 §3 **MUST** 报 \(L/M\)、\(H/M\)、\((H-L)/2M\)（`neutral` 档宽占 M 的比例）；三者的仓库分布由 `scripts/calibration.py` 输出。
主尺形如「分子 × 倍数」时，\(L/M\) 是**分子比 × 倍数比**的乘积，**MUST 分解报出**；**跨票比较用分子比**，未分解的 \(L/M\) 比较的是作者的倍数习惯而非情景。倍数政策按林奇主类的默认见 skill A2e-5——`cyclical` 应**反向或 flat**（林奇：谷底盈利配高倍数），同向双压是把周期算两遍。散布 **MUST** 可归因到基本面或林奇分类，**MUST NOT** 来自构造手法。

**共享情景轴**：多票落在同一条宏观或产业驱动因子上时（金铜价格梯、AI capex 路径、利率路径…），**MUST** 共用同一条情景梯，由各自 `eval/` 跨文件逐格校验，单改一边即红；各票只保留自己对该梯的**敏感度**，**MUST NOT** 各造各的下行。

轴落盘为 `frameworks/_AXIS.<轴名>.md`，内含三档值、每档具名来源与 as-of、传导规则、跨票加总校验。轴只锁**共享的那一层**——当轴到分子之间还隔着份额或单价时（`_AXIS.ai-capex.md` 即如此），第二层 **MUST** 由各票逐条命名并进 Assumptions，**MUST NOT** 塞回轴里冒充宏观下行。

价带与 zone 由脚本演算；弱 LLM 至多做归因。**MUST NOT** 让弱流水线擅自改 `frameworks/*.md` 或自行改概率锚点。  
标的文档 **MUST** 输出锚点 \(L/M/H\)、切点 \(A/B\)、以及**填入本票数字的五档价格区间表**与现行 `zone` id。  
**MUST NOT** 在标的文档重复本节约语义长文；规则真源在本节，标的文档只落数字区间。

日报：`scripts/daily_bands.py` 只读框架锚点 + Longbridge 现价 → 写 `output/bands/` → 飞书（**每天都发**；跨带 `@所有人`）。**MUST NOT** 回写框架 MD。

## 密钥与本地

- **MUST NOT** 提交 `.mcp.json`、`config/mcporter.json`、`_local/`
- Wisburg / 行情密钥只放在 gitignore 的本地配置里

## 不要做的事

- 不要在本仓做候选池挖掘或仓位执行建议冒充框架
- 不要把 invest 的 Theme/View YAML 当作本仓估值真源
- 不要编造无来源的财务数字
