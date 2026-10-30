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
- 「研究新票」「讨论已有框架」新建时可选 **opencode**（默认仍是 Claude Code）：每个活跃对话起一个 `opencode acp` 常驻进程（ACP 协议，orchestrator 工作方式，模型 0g 的 DeepSeek V4.1 Flash），沿用本机全局 opencode 配置，只经 `OPENCODE_CONFIG_CONTENT` 叠加权限与规则。bash 禁用清单与 Claude 对话同源（`chat/config.py`），写文件工具只限 `frameworks/`（插件自带、不经审批就改文件的 `ast_grep_replace` 一并禁用）；改 sediment-framework skill 只有总指挥能发起、推到手机审批，子 agent 一律拒绝（opencode 不把子 agent 的审批请求转给 app）。对话记录由服务端写在 `output/chat/opencode/<id>.jsonl`（Claude 记录同格式）；不支持编辑消息、压缩上下文、自动起标题；opencode 那边丢了原会话时自动新开一个并提示。启动参数与权限的唯一真源是 `app/server/lynch_reader/chat/opencode.py`；升级 opencode 或 oh-my-opencode-slim 后跑 `(cd app/server && LYNCH_LIVE_OPENCODE=1 uv run pytest tests/test_chat_opencode_live.py -q -s)`。
- 框架归档：在 app 文档页顶栏点「归档 / 恢复」，归档清单只存 `output/archive/frameworks.json`（服务端唯一写者），只影响 app 首页——默认隐藏已归档标的，勾「展示归档」才显示；框架文档、每票演算脚本、日报、`just check` 都不受影响，也不写 `frameworks/`。
- 财报提示：⋯ 菜单「财报」页列出框架池（不含已归档）里刚发布（发布日后 7 天内）和即将发布（3 天内）财报的票；待看（已出、没调研、没忽略）时 ⋯ 上亮「!」；菜单行写「财报 · N 待看 · M 将出」（将出 = 窗口内还没发布，不亮「!」），页面一行都没有时菜单里不出现「财报」（异动同理）。日历只用 `longbridge finance-calendar`（`eval/lib/earnings_lb.py`），服务端每天北京时间 16:37 后全量查一次（三个市场都不在正式时段）、新加的框架单独查、失败 30 分钟内不重试，缓存与忽略清单在 `output/earnings/`（服务端唯一写者）；「已出」按发布时段与市场时钟判定（盘前按开盘，其余按收盘），不等财务数据入库；长桥在实际数字录入后会改写记录（美股盘后的日期挪到北京那天、多出前三季度 / 去年同期条目、有的整条删掉），所以录了实际数字的记录按「时段时刻」与「电话会开始时间」中较早的算，页面日期与忽略都用校正后的日子，已出的发布被日历删掉时缓存照留到 7 天窗口结束。财报后开过该票讨论对话即算已调研。判定与缓存的唯一真源是 `app/server/lynch_reader/earnings.py`；调研提示词在 `app/web/src/earnings/prompts.ts`。飞书财报提醒已下线。
- 现价与异动提示：现价只有一份——服务端后台每 5 分钟取一次全池现价（池里没有市场在交易、也没有新框架时不取；没有手动取价的按钮；读列表时池里有还没取过价的新框架，就把这次全池取价提前，失败了留给后台；开盘竞价中——港股 9:00–9:22、A 股 9:15–9:25——的价是参考价不是成交，取到也按昨收算，这段时间不算交易），首页列表和异动判定都不自己取价，异动判定在每批现价到达时做。首页列表与文档不用手动刷新：进首页 / 文档页、停在这两种页面时 app 回到前台、停在首页每 5 分钟各问一次服务端，没变只回空的 304（按回答内容的摘要判断）；框架与轴文件按修改时间和大小缓存解析，只重解析改过的文件。⋯ 菜单「异动」页每只票列出最新一条：现价（含盘前、盘后、夜盘）相对它所在时段对应的正式收盘，超过平时波动 4 倍，或正式时段超过 2 倍且当天成交量超过平时 5 倍（平时 = 近 60 个交易日的日涨跌幅标准差 / 成交量中位数，日线由后台在各市场收盘后 23 分钟查一次——A 股 15:23、港股 16:23、美股美东 16:23，时刻在 `market_hours.py`）。首次触发时已在财报页上的票归财报，不进异动。判定与存储的唯一真源是 `app/server/lynch_reader/anomalies.py`，记录在 `output/anomalies/`（服务端唯一写者）；现价的唯一来源是 `catalog.py` 的 `QuoteCache` / `QuotePoller`。
- 观察区：讨论已有框架的对话建议先不落盘、等后续数据（卖方刷新预测、10-Q / 年报、说明会、同业财报、事件结果）时，回复末尾附一张 `lynch-watch` 卡片（每件要等的事带一个估计日子，模糊说法由 agent 换算成具体日子；格式写在两种 agent 共用的对话规则里，`chat/config.py` 的 `WATCH_RULE`）；用户勾选点「加入观察区」才存，只存这只票自己对话里的卡片（这只票已有卡片时新卡片默认不勾选，勾上才覆盖）。每只票一张卡片，再存就整张覆盖；只有用户点 × 才移出，不会自动消失。卡片里最早的日子（北京时间；美股盘后发布记北京次日）到了就算到期，到期且存入之后没开过该票讨论对话时 ⋯ 上亮「!」，菜单行写「观察 · N 到期 · M 等待」；点「调研」开讨论对话，提示词带上卡片内容，还没等到的事由 agent 给出新卡片覆盖。解析与存储的唯一真源是 `app/server/lynch_reader/chat/watch.py`，记录在 `output/watch/`（服务端唯一写者）；调研提示词在 `app/web/src/earnings/prompts.ts`。
- 候选区 `output/candidates/candidates.json`：新候选只在用户明确选择时进入——app 里点「存入候选区」（只存 Grok 原话里出现过的候选），终端里经 `just candidates-save` 点名（只存给定回答文件候选块里的票）；待看和已入库的票都能手动「归档」、在已归档里「恢复」；归档满 30 天的票从文件删除，已归档的票再次存入则回到待看；每只票显示 5 个不同 KOL 各自最新的一条来源，这几条永不回收，其余存入超过 7 天的来源只在这只票再次存入时回收（天数可用环境变量 `LYNCH_CANDIDATE_SOURCE_DAYS` 改，服务端和 `just candidates-save` 都读它；展开卡片可看全部来源）；来源按 X 帖子编号去重；看空的观点不进候选。服务端与脚本共用 `CandidateStore`，加文件锁读改写。不写 invest。新建发现新票默认 grok-4.5、500k。

## 密钥与本地

- **MUST NOT** 提交 `.mcp.json`、`config/mcporter.json`、`config/daily.toml`、`_local/`
- Wisburg / 行情密钥只放在 gitignore 的本地配置里
