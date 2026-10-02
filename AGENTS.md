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

`scripts/daily_bands.py` 只读框架锚点 + Longbridge 现价 → 写 `output/bands/` → 飞书。**MUST NOT** 回写 `frameworks/*.md`，也 **MUST NOT** 让弱流水线改框架或概率锚点。`output/bands/` 是日报带时间戳的观测日志（跨带比较用），不是框架现状；框架文档不写现价与现行 zone（`frameworks/AGENTS.md`「现价 — 禁写契约」）。

## app 对话

- app 服务端（`app/server`）自身不写 `frameworks/`、`output/bands/`、`../invest`；**对话**会起 Claude Code 子进程（`claude -p`，走本机登录的订阅额度），它像普通 Claude Code 研究会话一样可以跑任意 Bash（禁用清单除外：sudo、会丢工作或推出去的 git、`longbridge` 非查询命令），Write / Edit 只限 `frameworks/`，临时演算放 `frameworks/_local/tmp/`（不提交）。启动参数、白名单与禁用清单的唯一真源是 `app/server/lynch_reader/chat/config.py`。
- app 对话是用户在场、逐轮确认的研究对话，**不属于**框架契约所禁止的「弱流水线」；落盘仍须用户明确说「可以落盘」，并走 `sediment-framework`。
- 对话历史就是 Claude Code 的会话记录（`~/.claude/projects/<slug>/<id>.jsonl`，30 天清理）；`output/chat/index.json` 只是列表索引。
- 服务端只放行已配对设备：设备 cookie 用一次性配对码换取（`just app-pair [设备名]` 生成，10 分钟有效；`just app-devices` 查看，`just app-revoke <id>` 吊销，立即生效），存于 `output/app-auth/`（只存哈希，不提交）；仍只绑 tailnet 地址。
- 白名单与禁用清单是护栏，不是沙箱：agent 能在本机执行任意代码（`bash -c` 或脚本就能绕过禁用清单），所以只能由用户本人驱动对话。
- 白名单外的调用由服务端代答（`--permission-prompt-tool stdio`）：只有改 `frameworks/.claude/skills/sediment-framework/` 的 Edit / Write 推到手机逐处审批（看 diff 点允许 / 拒绝），其余一律拒绝。规则与 diff 在 `app/server/lynch_reader/chat/approvals.py`。
- app 对话的行情走 `longbridge` 命令行（和 `eval/lib`、日报共用它的登录），禁用清单拦下单、账户、登录与升级等非查询子命令（CLI 日后新增的子命令要补进清单）；登录过期时在终端 `longbridge auth login`。
- 「发现新票」对话走 **Grok**（`grok` 命令行，本机 grok.com 登录）：每轮起一个 `grok -p` 子进程，只能用 X 搜索和网页搜索——在独立空目录 `output/chat/grok-workdir/` 运行、关掉 Claude / Cursor 兼容加载、显式 deny 终端与读写文件与外部插件工具。启动参数与隔离清单的唯一真源是 `app/server/lynch_reader/chat/grok.py`；升级 grok 后跑 `(cd app/server && LYNCH_LIVE_GROK=1 uv run pytest tests/test_chat_grok_live.py -q -s)`。
- 发现新票的搜索规则与候选格式只在 skill `.agents/skills/discover-kol/`（`references/search.md`）：app 每轮把它、当场读出的 KOL 名单和用户原话拼成一条消息发给 Grok（`grok --rules` 对经 ACP 建、再 `-p --resume` 的会话不生效，所以不用它）；对话记录只存用户原话。只按 invest 配置的 X 列表搜（`list_id`，经 invest 自己的加载函数 `../invest/scripts/load_kol_handles.py` 每轮现读，lynch 不复制；成员由用户在 X 上维护，invest 的账号清单不用）。回看范围由用户新建对话时选（最近 1 / 2 / 3 / 7 天，默认 2 天），不用 invest 的回看时长。终端里用 grok 开会话时同一个 skill 自动可用（`just kol-sources`、`just candidates-save`）。
- Grok 对话记录由服务端写在 `output/chat/grok/<id>.jsonl`（Claude 记录同格式）；上下文长度在新建时经 ACP 选定。
- 候选区 `output/candidates/candidates.json`：新候选只在用户明确选择时进入——app 里点「存入候选区」（只存 Grok 原话里出现过的候选），终端里经 `just candidates-save` 点名（只存给定回答文件候选块里的票）；待看和已入库的票都能手动「归档」、在已归档里「恢复」；归档满 30 天的票从文件删除，已归档的票再次存入则回到待看；一只票名下存入超过 30 天的来源，只在这只票再次存入时回收；来源按 X 帖子编号去重；看空的观点不进候选。服务端与脚本共用 `CandidateStore`，加文件锁读改写。不写 invest。新建发现新票默认 grok-4.5、500k。

## 密钥与本地

- **MUST NOT** 提交 `.mcp.json`、`config/mcporter.json`、`config/daily.toml`、`_local/`
- Wisburg / 行情密钥只放在 gitignore 的本地配置里
