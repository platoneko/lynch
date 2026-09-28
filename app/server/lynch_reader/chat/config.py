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
# is allowed until it is listed here.
LONGBRIDGE_BLOCKED_COMMANDS: tuple[str, ...] = (
    "auth", "init", "check", "update", "tui", "completion", "subscriptions", "topic", "watchlist", "statement",
    "order", "assets", "cash-flow", "portfolio", "positions", "fund-positions", "margin-ratio", "max-qty", "alert",
    "profit-analysis", "dca", "sharelist", "bank-cards", "withdrawals", "deposits", "ipo",
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

_RULES = """\
You are working inside the lynch phone app; the user replies turn by turn from a phone.
- The contracts are not preloaded; read them when they apply. frameworks/AGENTS.md is the framework contract \
(anchors L/M/H, the five zones, no current price in framework files): read it before explaining a framework's \
anchors or zones. AGENTS.md at the repo root is the code repo's contract.
- Before changing any framework, read frameworks/AGENTS.md and follow the sediment-framework skill. \
Do not create or modify framework content (frameworks/*.md and the axes/ and eval/ directories) until the user \
explicitly says it may be written (e.g. 「可以落盘」).
- Scratch work needs no go-ahead: put throwaway scripts and notes under frameworks/_local/tmp/ and run them, or run \
Python inline with Bash. Never stage or commit frameworks/_local/.
- Edits to the sediment-framework skill (frameworks/.claude/skills/sediment-framework/) need the user's explicit \
go-ahead like a framework; after that, each Edit or Write shows the user its diff on the phone and waits for their \
answer. Other calls outside the whitelist are refused.
- For market data use the longbridge CLI with Bash, e.g. `longbridge quote AAPL.US --format json`; \
`longbridge help` lists the commands. Only query commands are allowed (no orders, account data or login).
- Write files only with the Write and Edit tools, and only under frameworks/. Run git on frameworks/ \
(`git -C frameworks …`); push, reset, checkout, restore, switch, stash, rebase and clean are refused.
- After landing a change, run `cd frameworks && just check`. When it passes, stage only the files this conversation changed and commit.
- The user may edit an earlier message, which truncates this conversation. File changes made in the truncated turns \
are not undone, so check the files and `git -C frameworks status` before acting on them.
- If a tool call is denied, do not work around it with another command; tell the user what was blocked."""


@dataclass(frozen=True)
class LaunchConfig:
    claude_cmd: tuple[str, ...]  # ("claude",); tests put a fake script here
    repo_root: Path  # every process runs here; .mcp.json lives here


def system_prompt(conv: Conversation) -> str:
    if conv.kind == "ticker":
        # The eval script is left unnamed: the framework's header points at it, and naming it here had the
        # agent read it up front in every chat (about 7k tokens) before any number was in question.
        scope = f"This conversation is about {conv.ticker}: its framework is frameworks/{conv.ticker}.md."
    else:
        scope = ("This conversation researches a new ticker that has no framework yet; once landed it becomes "
                 "frameworks/<TICKER>.md with frameworks/eval/<TICKER>.py.")
    return f"{scope}\n{_RULES}"


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
    return env
