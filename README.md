# lynch

长线投资的**估值框架沉淀**仓库：林奇分类 → 专属属性与可证伪假设 → 可复现演算块。  
标的由你指定；挖掘在 [`../invest`](../invest)。

## 怎么沉淀一只票

在本仓库打开 Claude / Grok / Codex，然后任选：

- 说：`给 INTC.US 写估值框架` / `按林奇沉淀紫金的框架` / `财报后更新 INTC.US 假设`
- 或显式：`/sediment-framework` · `/lynch-framework`

Agent 应加载 skill **`sediment-framework`**：**先持续对话**对齐分类 / thesis / 主尺 / 假设 / 价带公式；你明确说「可以落盘」后，才按模板写入：

- `frameworks/<TICKER>.md` — 真源（题头含林奇类 + 主尺一行）

模板：`frameworks/_TEMPLATE.md`。协作硬约束：`AGENTS.md`。  
标的池 = 扫 `frameworks/*.md`（排除 `_` 前缀）；**不**维护 `universe.yaml`。

## 目录

| 路径 | 说明 |
|------|------|
| `frameworks/` | 单票框架（题头索引 + 现行结论 + Assumptions + Changelog） |
| `.claude/skills/sediment-framework/` | 落盘流程 skill（中文） |
| `.claude/skills/*` | finance-skills 软链（取数） |
| `.mcp.json` | Wisburg MCP（本地，gitignore） |
| `_local/` | 个人草稿，不入库 |

## 林奇六类

`slow_grower` · `stalwart` · `fast_grower` · `cyclical` · `turnaround` · `asset_play`  

主类必填；次类可选且仍属六类。专属叙事只写在框架正文。

## 五档价带（项目统一）

框架算出锚点 \(L < M < H\)，用中点切成五档 zone：

| zone | 中文 | 区间 |
|------|------|------|
| `deep_discount` | 深度折价 | \(P < L\) |
| `conservative` | 保守 | \([L,(L+M)/2)\) |
| `neutral` | 中性 | \([(L+M)/2,(M+H)/2)\) |
| `optimistic` | 乐观 | \([(M+H)/2,H]\) |
| `stretched` | 叙事溢价 | \(P > H\) |

细则与语义见 `AGENTS.md`。单票如何得到 L/M/H 写在各自 `frameworks/*.md`（如 `INTC.US` 为不对称 SOTP）。

## 日报价带 + 飞书

每天扫 `frameworks/*.md` 的 \(L/M/H\)，用 Longbridge 常规 `last` 归 zone，写入 `output/bands/<TICKER>.json`（gitignore），并飞书推送：

- **每天**：一条飞书 **post 富文本**全池日报，每票 2–3 行，形如  
  `🟡 603993.SH 洛阳钼业 19.01 CNY`  
  `「中性」 L/M/H = 13.51/19.63/23.24`  
  有 \(V_{opt}>0\) 时再加一行修正带：`「乐观」 +V(66.6) L/M/H = 27.2/71.17/113`
- **跨带**（相对上次 bands 的底牌 `zone` **或** `option_zone` 变化，且两侧都已有上次读数）：另发一条 post，带 `@所有人`；底牌跨带写 `「中性」 → 「乐观」`，期权跨带写 `+V 「中性」 → 「乐观」`（可同时出现）

```bash
# dry-run：不写盘、不 POST
python3 scripts/daily_bands.py --dry-run

# 实跑：默认复用 ../invest 的飞书 webhook（policies.toml [alerts].feishu_webhook）
# 也可设 FEISHU_ALERT_WEBHOOK，或在 config/daily.toml 覆盖（见 example）
python3 scripts/daily_bands.py

# 只算价带、不推飞书
python3 scripts/daily_bands.py --no-notify
```

安装工作日 cron（`Asia/Shanghai`：`09:03` 早盘 + `16:13` 午后；脚本内 as_of 同此时区）：

```bash
bash scripts/install-daily-cron.sh
```

**MUST NOT** 让日报改写 `frameworks/*.md`。单票完整公式校验仍用 `python3 eval/<TICKER>.py`。
