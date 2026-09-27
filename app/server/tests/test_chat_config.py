from __future__ import annotations

from pathlib import Path

import pytest

from lynch_reader.chat.config import ALLOWED_TOOLS, LaunchConfig, launch_argv, launch_env, system_prompt
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
    assert len(ALLOWED_TOOLS) == 19
    assert "Bash(git -C frameworks commit *)" in ALLOWED_TOOLS
    assert not [t for t in ALLOWED_TOOLS if t.startswith("Bash(git ") and "-C frameworks" not in t]
    assert "Write(frameworks/**)" in ALLOWED_TOOLS and "Write" not in ALLOWED_TOOLS


def test_env_drops_api_billing_and_claudeai_connectors():
    env = launch_env({"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "u",
                      "CLAUDE_CODE_USE_BEDROCK": "1"})
    assert env == {"PATH": "/bin", "HOME": "/h", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


def test_ticker_prompt_names_its_files_and_rules():
    p = system_prompt(conv(kind="ticker", ticker="NVDA.US"))
    assert "frameworks/NVDA.US.md" in p and "frameworks/eval/NVDA.US.py" in p
    assert "git -C frameworks" in p and "sediment-framework" in p


def test_research_prompt_has_no_ticker():
    p = system_prompt(conv(kind="research", ticker=None))
    assert "new ticker" in p and "None" not in p
