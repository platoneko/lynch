from __future__ import annotations

from pathlib import Path

import pytest

from lynch_reader.chat.config import (
    ALLOWED_TOOLS, DENIED_TOOLS, LaunchConfig, launch_argv, launch_env, system_prompt,
)
from tests.chat_helpers import conv

CFG = LaunchConfig(claude_cmd=("claude",), repo_root=Path("/repo"))


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_new_conversation_uses_its_id_as_session_id():
    argv = launch_argv(CFG, conv(), resume=False)
    assert argv[:2] == ["claude", "-p"]
    assert flag(argv, "--session-id") == "c1"
    assert "--resume" not in argv


def test_resume_and_truncate():
    argv = launch_argv(CFG, conv(), resume=True, resume_at="u9")
    assert flag(argv, "--resume") == "c1"
    assert flag(argv, "--resume-session-at") == "u9"
    assert "--session-id" not in argv


def test_truncate_needs_resume():
    with pytest.raises(ValueError):
        launch_argv(CFG, conv(), resume=False, resume_at="u9")


def test_stream_json_both_ways_with_partials_and_replay():
    argv = launch_argv(CFG, conv(), resume=False)
    assert flag(argv, "--input-format") == "stream-json"
    assert flag(argv, "--output-format") == "stream-json"
    for f in ("--verbose", "--include-partial-messages", "--replay-user-messages"):
        assert f in argv


def test_model_effort_settings_and_mcp():
    argv = launch_argv(CFG, conv(model="claude-haiku-4-5", effort="low"), resume=False)
    assert flag(argv, "--model") == "claude-haiku-4-5"
    assert flag(argv, "--effort") == "low"
    assert flag(argv, "--setting-sources") == "project"
    assert flag(argv, "--mcp-config") == "/repo/.mcp.json"
    assert "--strict-mcp-config" in argv


def test_whitelist_is_the_last_variadic_flag():
    argv = launch_argv(CFG, conv(), resume=False)
    assert tuple(argv[argv.index("--allowedTools") + 1:]) == ALLOWED_TOOLS


def test_denylist_precedes_the_whitelist():
    argv = launch_argv(CFG, conv(), resume=False)
    assert tuple(argv[argv.index("--disallowedTools") + 1:argv.index("--allowedTools")]) == DENIED_TOOLS


def test_bash_is_open_but_files_are_written_only_under_frameworks():
    # user, 2026-09-28: research runs like a normal session; Write / Edit stay scoped so skill edits hit the phone
    assert "Bash(*)" in ALLOWED_TOOLS
    assert "Write(frameworks/**)" in ALLOWED_TOOLS and "Write" not in ALLOWED_TOOLS and "Edit" not in ALLOWED_TOOLS
    assert "Bash(sudo *)" in DENIED_TOOLS
    for c in ("push", "reset", "checkout", "stash"):
        assert f"Bash(git *{c}*)" in DENIED_TOOLS, c
    assert not [t for t in DENIED_TOOLS if "commit" in t or "add" in t]


def test_longbridge_is_the_cli_with_query_commands_only():
    # user, 2026-09-28: the CLI's own login instead of MCP OAuth; no orders, account data, login or self-update
    assert not [t for t in ALLOWED_TOOLS if t.startswith("mcp__longbridge")]
    for c in ("order", "auth", "update", "positions", "assets", "watchlist", "alert", "dca", "topic", "sharelist"):
        assert f"Bash(longbridge {c}*)" in DENIED_TOOLS, c
    for c in ("quote", "financial-report", "kline", "help"):
        assert not [t for t in DENIED_TOOLS if t.startswith(f"Bash(longbridge {c}")], c


def test_env_drops_api_billing_and_claudeai_connectors():
    env = launch_env({"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "u",
                      "CLAUDE_CODE_USE_BEDROCK": "1"})
    assert env == {"PATH": "/bin", "HOME": "/h", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


def test_ticker_prompt_names_its_files_and_rules():
    p = system_prompt(conv(kind="ticker", ticker="NVDA.US"))
    assert "frameworks/NVDA.US.md" in p
    assert "frameworks/eval/" not in p  # read only when the numbers are checked, not up front
    assert "git -C frameworks" in p and "sediment-framework" in p
    # `claude -p` with project-only settings does not load AGENTS.md, so the prompt names the contracts
    assert "frameworks/AGENTS.md" in p and "仓库根目录的 AGENTS.md" in p
    assert "longbridge quote" in p


def test_research_prompt_has_no_ticker():
    p = system_prompt(conv(kind="research", ticker=None))
    assert "新标的" in p and "None" not in p


def test_permission_prompts_come_to_the_app():
    # Without it claude refuses every .claude/ write outright; with it, the server answers (chat/approvals.py).
    argv = launch_argv(CFG, conv(), resume=False)
    assert flag(argv, "--permission-prompt-tool") == "stdio"
    assert argv.index("--permission-prompt-tool") < argv.index("--allowedTools")


def test_prompt_explains_skill_approvals():
    p = system_prompt(conv(kind="research", ticker=None))
    assert "frameworks/.claude/skills/sediment-framework/" in p and "diff 推到用户手机上" in p


def test_prompt_frees_scratch_but_not_framework_content():
    p = system_prompt(conv(kind="research", ticker=None))
    assert "frameworks/_local/tmp/" in p and "永远不要 stage 或提交 frameworks/_local/" in p
    assert "「可以落盘」" in p
