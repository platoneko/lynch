"""How the app launches Claude Code for a chat: models, tool whitelist, argv, environment, appended prompt.

Flags were verified against Claude Code 2.1.283 on 2026-09-27 (spec §9).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lynch_reader.chat.index import Conversation

CHAT_MODELS: tuple[tuple[str, str], ...] = (
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-5", "Sonnet 5"),
    ("claude-fable-5-1", "Fable 5.1"),
    ("claude-haiku-4-5", "Haiku 4.5"),
)
CHAT_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"

IDLE_TIMEOUT_S = 3600.0  # the prompt cache lives 1 h, so a lazy resume within it mostly hits the cache
MAX_PROCESSES = 8

# Market data comes from the longbridge CLI on its own login (the one eval/lib and the daily report use), not
# the longbridge MCP, which needs a separate OAuth (user, 2026-09-28). Everything but queries is denied: the CLI
# can also place orders, read the account, edit watchlists/alerts/DCA plans, post community topics, log out and
# self-update, and nobody has confirmed its login is a paper account. A subcommand added by a later CLI version
# is allowed until it is listed here (last reviewed against 0.29.0). `fund` goes whole: its fund orders and
# positions sit beside the NAV queries, and the prefix also covers `fund-positions`. `serve` / `acp` / `agent` /
# `ai` reach the account or trading through a long-lived or interactive process.
LONGBRIDGE_BLOCKED_COMMANDS: tuple[str, ...] = (
    "auth", "init", "check", "update", "tui", "completion", "subscriptions", "topic", "watchlist", "statement",
    "order", "assets", "cash-flow", "portfolio", "positions", "fund", "margin-ratio", "max-qty", "alert",
    "profit-analysis", "dca", "sharelist", "bank-cards", "withdrawals", "deposits", "ipo",
    "grid", "serve", "acp", "agent", "ai",
)
# Git that moves or discards work the user may have in progress in frameworks/, or pushes it out. The middle
# `*` also catches `git -C <dir> …`.
GIT_BLOCKED_COMMANDS: tuple[str, ...] = (
    "push", "reset", "clean", "checkout", "restore", "switch", "stash", "rebase",
)

# Research runs like a normal Claude Code session (user, 2026-09-28: 「就像在 neige 上调研一样」): any Bash, so
# scratch math can be run inline or from frameworks/_local/tmp/. Deny rules win over Bash(*) and are checked on
# every part of a pipe or `&&` chain (verified 2026-09-28), but `bash -c '…'` or a script gets past them: a
# guardrail, not a sandbox. Write / Edit stay inside frameworks/ so skill edits still reach the phone.
ALLOWED_TOOLS: tuple[str, ...] = (
    "Read", "Glob", "Grep", "Skill", "WebSearch", "WebFetch",
    "Write(frameworks/**)", "Edit(frameworks/**)",
    "mcp__wisburg__*",
    "Bash(*)",
)
DENIED_TOOLS: tuple[str, ...] = (
    "Bash(sudo *)",
    *(f"Bash(git *{c}*)" for c in GIT_BLOCKED_COMMANDS),
    *(f"Bash(longbridge {c}*)" for c in LONGBRIDGE_BLOCKED_COMMANDS),
)

# Any of these would move billing from the Max subscription to the API.
API_BILLING_ENV: tuple[str, ...] = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
)

# The rules every app chat gets, whichever agent runs it; each agent appends the lines naming its own tools
# (CLAUDE_TOOL_RULES here, chat/opencode.py for opencode).
SHARED_RULES = """\
你在 lynch 手机 app 里工作；用户在手机上逐轮回复。
- 始终用简体中文和用户对话，第一轮就用中文，不论用户消息多短、读到的资料是什么语言。
- 契约没有预加载，用到时再读。frameworks/AGENTS.md 是框架契约（锚点 L/M/H、五档 zone、框架文件不写现价）：\
解释某个框架的锚点或 zone 之前先读它。仓库根目录的 AGENTS.md 是代码仓库的契约。
- 改任何框架之前，先读 frameworks/AGENTS.md，并按 sediment-framework skill 走。\
在用户明确说可以写（例如「可以落盘」）之前，不要创建或修改框架内容（frameworks/*.md 以及 axes/、eval/ 目录）。
- 临时演算不用等许可：一次性脚本和笔记放 frameworks/_local/tmp/ 下运行，或用 Bash 直接跑 Python。\
永远不要 stage 或提交 frameworks/_local/。
- 行情用 Bash 跑 longbridge 命令行，例如 `longbridge quote AAPL.US --format json`；`longbridge help` 列出全部命令。\
只允许查询类命令（不能下单、看账户或登录）。
- 落盘改动之后跑 `cd frameworks && just check`。通过后只 stage 本次对话改过的文件并提交。
- 工具调用被拒绝时，不要换个命令绕过去；告诉用户什么被拦了。"""
GIT_RULE_TAIL = ("对 frameworks/ 跑 git 用 `git -C frameworks …`；"
                 "push、reset、checkout、restore、switch、stash、rebase、clean 会被拒绝。")
CLAUDE_TOOL_RULES = f"""\
- WebFetch 读不出的 PDF：用 Read 按页直接读它在 ~/.claude/ 下的缓存文件，或用 curl 从原网址下载到 \
frameworks/_local/tmp/ 再处理；不要用 cp 复制 ~/.claude/ 下的缓存（Claude Code 把那里当敏感目录，一定会被拒绝）。
- 改 sediment-framework skill（frameworks/.claude/skills/sediment-framework/）和改框架一样，要用户明确同意；\
同意后，每次 Edit 或 Write 都会把 diff 推到用户手机上，等用户答复。白名单外的其他调用一律拒绝。
- 写文件只用 Write 和 Edit 工具，且只写 frameworks/ 下。{GIT_RULE_TAIL}
- 用户可能编辑之前的消息，这会截断本次对话。被截掉的轮次里做过的文件改动不会撤销，\
所以基于这些文件行动前，先检查文件和 `git -C frameworks status`。"""


@dataclass(frozen=True)
class LaunchConfig:
    claude_cmd: tuple[str, ...]  # ("claude",); tests put a fake script here
    repo_root: Path  # every process runs here; .mcp.json lives here


# Post-earnings checks often end in "wait for the sell-side to revise" with a date only the chat remembered
# (user, 2026-10-30): the card the app parses (chat/watch.py) puts the ticker on the watch list until it is due.
WATCH_RULE = """\
- 建议先不落盘、等后续数据时（卖方按本期财报刷新预测、10-Q / 年报、业绩说明会、同业财报、某个事件出结果等），\
在回复末尾附一张观察区卡片，用户在手机上点「加入观察区」才会存：
```lynch-watch
{"ticker": "%s", "waits": [{"what": "等什么（括号里保留原来的时间说法，如「约一两周」）", "date": "YYYY-MM-DD"}], \
"recheck": "到时要重新核对的数字或假设，一两句"}
```
  每件要等的事一条；date 是你估计它出来的那天，模糊说法按今天的北京日期换算成具体日子（不确定今天几号就跑 `date`）。\
存入会覆盖这只票在观察区的旧卡片，所以把仍在等的事全部列上。一次回复最多一张；不需要等就不附。"""


def scope_line(conv: Conversation) -> str:
    if conv.kind == "ticker":
        # The eval script is left unnamed: the framework's header points at it, and naming it here had the
        # agent read it up front in every chat (about 7k tokens) before any number was in question.
        return f"本次对话关于 {conv.ticker}：它的框架是 frameworks/{conv.ticker}.md。\n{WATCH_RULE % conv.ticker}"
    return ("本次对话研究一个还没有框架的新标的；落盘后它会成为 "
            "frameworks/<TICKER>.md 和 frameworks/eval/<TICKER>.py。")


def system_prompt(conv: Conversation) -> str:
    return f"{scope_line(conv)}\n{SHARED_RULES}\n{CLAUDE_TOOL_RULES}"


def launch_argv(cfg: LaunchConfig, conv: Conversation, *, resume: bool, resume_at: str | None = None) -> list[str]:
    if resume_at is not None and not resume:
        raise ValueError("--resume-session-at needs --resume")
    session = ["--resume", conv.id] if resume else ["--session-id", conv.id]
    if resume_at is not None:
        session += ["--resume-session-at", resume_at]
    return [
        *cfg.claude_cmd, "-p",
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--include-partial-messages", "--replay-user-messages",
        *session,
        "--model", conv.model, "--effort", conv.effort,
        # Project layer only: .claude/settings.local.json allows broad Bash that would bypass the whitelist.
        "--setting-sources", "project",
        "--mcp-config", str(cfg.repo_root / ".mcp.json"), "--strict-mcp-config",
        # Calls the whitelist does not cover come to the app (chat/approvals.py): skill edits wait for the
        # phone, the rest are refused. Without it claude refuses every write under .claude/ outright.
        "--permission-prompt-tool", "stdio",
        "--append-system-prompt", system_prompt(conv),
        "--disallowedTools", *DENIED_TOOLS,  # variadic: ends at the next flag
        "--allowedTools", *ALLOWED_TOOLS,  # variadic: must stay last
    ]


def launch_env(base: Mapping[str, str]) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in API_BILLING_ENV}
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"  # the account's Drive / Docs connectors are not research tools
    # Edit(frameworks/**) and Write(frameworks/**) resolve against the session's working directory, which a
    # Bash `cd frameworks && …` moves for the rest of the run; returning to the repo root after each Bash call
    # keeps the whitelist meaning frameworks/ (verified against Claude Code 2.1.284 on 2026-09-30).
    env["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"] = "1"
    return env
