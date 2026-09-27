from __future__ import annotations

from pathlib import Path

import pytest

from lynch_reader.chat.config import (
    ALLOWED_TOOLS, LONGBRIDGE_QUERY_COMMANDS, LaunchConfig, launch_argv, launch_env, system_prompt,
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


def test_whitelist_matches_the_verified_rules():
    # spec §9 第 2 条: plain `git …` rules would never match (git must run as `git -C frameworks …`)
    assert len(ALLOWED_TOOLS) == 18 + len(LONGBRIDGE_QUERY_COMMANDS)
    assert "Bash(git -C frameworks commit *)" in ALLOWED_TOOLS
    assert not [t for t in ALLOWED_TOOLS if t.startswith("Bash(git ") and "-C frameworks" not in t]
    assert "Write(frameworks/**)" in ALLOWED_TOOLS and "Write" not in ALLOWED_TOOLS


def test_longbridge_is_the_cli_with_query_commands_only():
    # user, 2026-09-28: the CLI's own login instead of MCP OAuth; no orders, account data, login or self-update
    assert not [t for t in ALLOWED_TOOLS if t.startswith("mcp__longbridge")]
    assert "Bash(longbridge quote*)" in ALLOWED_TOOLS and "Bash(longbridge financial-report*)" in ALLOWED_TOOLS
    for c in ("order", "auth", "update", "positions", "assets", "watchlist", "alert", "dca", "topic", "sharelist"):
        assert not [t for t in ALLOWED_TOOLS if t.startswith(f"Bash(longbridge {c}")], c


def test_env_drops_api_billing_and_claudeai_connectors():
    env = launch_env({"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "u",
                      "CLAUDE_CODE_USE_BEDROCK": "1"})
    assert env == {"PATH": "/bin", "HOME": "/h", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


def test_ticker_prompt_names_its_files_and_rules():
    p = system_prompt(conv(kind="ticker", ticker="NVDA.US"))
    assert "frameworks/NVDA.US.md" in p
    assert "frameworks/eval/" not in p  # read only when the numbers are checked, not up front
    assert "git -C frameworks" in p and "sediment-framework" in p
    assert "longbridge quote" in p


def test_research_prompt_has_no_ticker():
    p = system_prompt(conv(kind="research", ticker=None))
    assert "new ticker" in p and "None" not in p
