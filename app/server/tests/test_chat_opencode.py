from __future__ import annotations

import json
from pathlib import Path

from lynch_reader.chat import config
from lynch_reader.chat.config import GIT_BLOCKED_COMMANDS, LONGBRIDGE_BLOCKED_COMMANDS
from lynch_reader.chat.opencode import (
    OpencodeConfig, claude_tool, config_overlay, mcp_servers, opencode_argv, opencode_env, permission_config,
    system_prompt, write_rules,
)
from tests.chat_helpers import conv

SKILL = "frameworks/.claude/skills/sediment-framework/**"


def cfg(tmp_path: Path) -> OpencodeConfig:
    return OpencodeConfig(("opencode",), tmp_path, tmp_path / "logs")


def test_argv_runs_acp():
    assert opencode_argv(OpencodeConfig(("oc",), Path("/r"), Path("/l"))) == ["oc", "acp"]


def test_permission_denies_the_same_commands_as_claude():
    bash = permission_config("deny")["bash"]
    assert next(iter(bash.items())) == ("*", "allow")  # first: the denies after it win (last match wins)
    assert bash["sudo *"] == "deny"
    for c in GIT_BLOCKED_COMMANDS:
        assert bash[f"git *{c}*"] == "deny"
    for c in LONGBRIDGE_BLOCKED_COMMANDS:
        assert bash[f"longbridge {c}*"] == "deny"


def test_permission_writes_only_frameworks():
    perm = permission_config("deny")
    assert list(perm["edit"].items()) == [("*", "deny"), ("frameworks/**", "allow"), (SKILL, "deny")]
    assert perm["external_directory"] == "deny"


def test_only_the_orchestrator_may_ask_for_skill_edits():
    # A subagent's ask never reaches the ACP client and hangs the turn (spike §4): subagents are denied.
    overlay = config_overlay(Path("/l/c1.rules.md"))
    assert overlay["permission"] == permission_config("deny")
    orchestrator = overlay["agent"]["orchestrator"]["permission"]
    assert orchestrator == {"bash": permission_config("ask")["bash"], "edit": permission_config("ask")["edit"]}
    assert orchestrator["edit"][SKILL] == "ask"
    assert overlay["instructions"] == ["/l/c1.rules.md"]


def test_env_overlays_config_and_keeps_the_rest(tmp_path):
    rules = tmp_path / "c1.rules.md"
    env = opencode_env({"PATH": "/bin", "HOME": "/h", "OPENCODE_CONFIG_CONTENT": "{}"}, rules)
    assert env["PATH"] == "/bin" and env["HOME"] == "/h"
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == config_overlay(rules)
    assert env["OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"] == "1"  # ~/.claude/CLAUDE.md is the user's, not the app's
    # Either would also drop .claude/skills, where sediment-framework comes from.
    assert "OPENCODE_DISABLE_CLAUDE_CODE" not in env and "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS" not in env


def test_rules_file_holds_the_prompt(tmp_path):
    c = conv(kind="ticker", ticker="NVDA.US")
    path = write_rules(cfg(tmp_path), c)
    assert path == tmp_path / "logs" / "c1.rules.md"
    assert path.read_text(encoding="utf-8") == system_prompt(c)


def test_rules_share_the_core_and_name_opencode_tools():
    claude, oc = config.system_prompt(conv()), system_prompt(conv())
    assert config.SHARED_RULES in claude and config.SHARED_RULES in oc
    assert oc.startswith(config.scope_line(conv()))
    assert "frameworks/AAA.US.md" in oc
    assert "~/.claude/" not in oc  # the Claude-cache PDF hint is Claude-only
    assert "Write 和 Edit" not in oc and "edit 和 write" in oc
    assert "子 agent" in oc  # skill edits only by the main agent


def test_claude_tool_maps_opencode_inputs_to_claude_shape():
    assert claude_tool("edit", {"filePath": "/r/frameworks/X.md", "oldString": "a", "newString": "b"}) == \
        ("Edit", {"file_path": "/r/frameworks/X.md", "old_string": "a", "new_string": "b", "replace_all": False})
    assert claude_tool("edit", {"filePath": "/r/X.md", "oldString": "a", "newString": "b", "replaceAll": True})[1][
        "replace_all"] is True
    assert claude_tool("write", {"filePath": "/r/f.md", "content": "c"}) == ("Write", {"file_path": "/r/f.md",
                                                                                       "content": "c"})
    assert claude_tool("read", {"filePath": "/r/f.md", "limit": 5}) == ("Read", {"file_path": "/r/f.md", "limit": 5})
    assert claude_tool("bash", {"command": "ls", "cwd": "/r"}) == ("Bash", {"command": "ls"})
    assert claude_tool("grep", {"pattern": "zone", "path": "/r"}) == ("Grep", {"pattern": "zone", "path": "/r"})
    assert claude_tool("glob", {"pattern": "*.md"}) == ("Glob", {"pattern": "*.md"})
    assert claude_tool("webfetch", {"url": "https://x"}) == ("WebFetch", {"url": "https://x"})
    assert claude_tool("websearch_web_search_exa", {"query": "q", "objective": "o"}) == ("WebSearch", {"query": "q"})
    assert claude_tool("skill", {"name": "sediment-framework"}) == ("Skill", {"skill": "sediment-framework"})
    task = {"description": "d", "subagent_type": "fixer", "prompt": "p"}
    assert claude_tool("task", task) == ("Agent", task)
    assert claude_tool("wisburg_list-feed", {"n": 1}) == ("wisburg_list-feed", {"n": 1})


def test_mcp_servers_empty_without_file(tmp_path):
    assert mcp_servers(tmp_path) == []


def test_mcp_servers_in_acp_shape(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "wisburg": {"type": "http", "url": "https://w/mcp", "headers": {"Authorization": "Bearer k"}},
        "local": {"command": "npx", "args": ["srv"], "env": {"TOKEN": "t"}},
    }}), encoding="utf-8")
    assert mcp_servers(tmp_path) == [
        {"type": "http", "name": "wisburg", "url": "https://w/mcp", "headers": [{"name": "Authorization",
                                                                                 "value": "Bearer k"}]},
        {"name": "local", "command": "npx", "args": ["srv"], "env": [{"name": "TOKEN", "value": "t"}]},
    ]
