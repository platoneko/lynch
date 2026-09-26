# lynch — 开发契约

lynch 的**代码仓库**：估值 / 解析 / zone 规则库 `eval/lib/`、日报与体检脚本 `scripts/`、手机阅读 app `app/`。
框架内容在另一个仓库 `lynch-frameworks`，检出在本仓库的 `frameworks/`（`.gitignore` 忽略）。

面向用户的说明用**简体中文**。代码标识符、`formula_id`、林奇枚举值用英文。

## 写框架前必读

@frameworks/AGENTS.md

沉淀、更新、纠正任何 `frameworks/` 下的内容之前，**MUST** 先读 `frameworks/AGENTS.md`（框架契约）并使用 skill `sediment-framework`（经 `.claude/skills` 软链可见）。

## 两个仓库

| 改什么 | 在哪提交 |
|--------|----------|
| `eval/lib/`、`scripts/`、`app/`、`justfile` | 本仓库（`lynch/` 根） |
| 框架 `.md`、`axes/`、每票演算 `frameworks/eval/<TICKER>.py`、skill、`_INDEX.md` | `frameworks/` 里（内容仓库） |

- 每票脚本 **MUST** 放在 `frameworks/eval/`；本仓库 `eval/` 顶层只放库（有测试守着）。
- 库改动若改变框架解析或 zone 结果，**MUST** 在 `frameworks/` 里跑 `just check`，并在本仓库跑全部测试。
- `frameworks/` 缺失时库直接报错；读真实框架的测试自动跳过。
- 在 git worktree 里改库：先在 worktree 根执行 `ln -s ~/plat1ko/lynch/frameworks frameworks`（已被忽略），否则读真实框架的测试只会跳过、`@frameworks/AGENTS.md` 也读不到。

## 测试

```bash
python3 -m unittest discover -s eval/lib -t . -p 'test_*.py'
(cd app/server && uv run pytest -q)
(cd frameworks && just check)      # 全部框架可解析 + 每票演算跑通
```

## 日报

`scripts/daily_bands.py` 只读框架锚点 + Longbridge 现价 → 写 `output/bands/` → 飞书。**MUST NOT** 回写 `frameworks/*.md`，也 **MUST NOT** 让弱流水线改框架或概率锚点。

## 密钥与本地

- **MUST NOT** 提交 `.mcp.json`、`config/mcporter.json`、`config/daily.toml`、`_local/`
- Wisburg / 行情密钥只放在 gitignore 的本地配置里
