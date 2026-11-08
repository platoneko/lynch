from __future__ import annotations

import json
from pathlib import Path

from lynch_reader.chat import config
from lynch_reader.chat.config import GIT_BLOCKED_COMMANDS, LONGBRIDGE_BLOCKED_COMMANDS
from lynch_reader.chat.opencode import (
    OpencodeConfig, claude_tool, config_overlay, v2_mcp_servers as mcp_servers, opencode_argv, opencode_env, permission_config,
    system_prompt, write_rules,
)
from tests.chat_helpers import conv

SKILL = "frameworks/.claude/skills/sediment-framework/**"


def cfg(tmp_path: Path) -> OpencodeConfig:
    return OpencodeConfig(("opencode",), tmp_path, tmp_path / "logs")


def test_argv_runs_v2_private_server():
    assert opencode_argv(OpencodeConfig(("oc",), Path("/r"), Path("/l"))) == ["oc", "serve"]


def test_native_question_forms_are_denied_in_app_global_and_orchestrator_rules():
    overlay = config_overlay(Path("/logs/rules.md"))
    for rules in (overlay["permissions"], overlay["agents"]["orchestrator"]["permissions"]):
        matches = [rule for rule in rules if rule["action"] in ("*", "question") and rule["resource"] == "*"]
        assert matches[-1]["effect"] == "deny"


def test_permission_denies_the_same_commands_as_claude():
    rules = permission_config("deny")
    assert rules[0] == {"action": "*", "resource": "*", "effect": "allow"}
    shell = {r["resource"]: r["effect"] for r in rules if r["action"] == "shell"}
    assert shell["sudo *"] == "deny"
    for c in GIT_BLOCKED_COMMANDS:
        assert shell[f"git *{c}*"] == "deny"
    for c in LONGBRIDGE_BLOCKED_COMMANDS:
        assert shell[f"longbridge {c}*"] == "deny"


def test_permission_writes_only_frameworks():
    rules = permission_config("deny")
    edits = [(r["resource"], r["effect"]) for r in rules if r["action"] == "edit"]
    assert edits == [("*", "deny"), ("frameworks/**", "allow"), (SKILL, "deny")]
    assert {"action": "external_directory", "resource": "*", "effect": "deny"} in rules
    assert {"action": "ast_grep_replace", "resource": "*", "effect": "deny"} in rules


def test_only_the_orchestrator_may_ask_for_skill_edits():
    overlay = config_overlay(Path("/l/c1.rules.md"))
    assert overlay["permissions"] == permission_config("deny")
    orchestrator = overlay["agents"]["orchestrator"]["permissions"]
    assert orchestrator == permission_config("ask")
    assert {"action": "edit", "resource": SKILL, "effect": "ask"} in orchestrator
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
    assert claude_tool("edit", {"path": "/r/frameworks/X.md", "oldString": "a", "newString": "b"}) == \
        ("Edit", {"file_path": "/r/frameworks/X.md", "old_string": "a", "new_string": "b", "replace_all": False})
    assert claude_tool("edit", {"path": "/r/X.md", "oldString": "a", "newString": "b", "replaceAll": True})[1][
        "replace_all"] is True
    assert claude_tool("write", {"path": "/r/f.md", "content": "c"}) == ("Write", {"file_path": "/r/f.md",
                                                                                       "content": "c"})
    assert claude_tool("read", {"path": "/r/f.md", "limit": 5}) == ("Read", {"file_path": "/r/f.md", "limit": 5})
    assert claude_tool("shell", {"command": "ls", "workdir": "/r"}) == ("Bash", {"command": "ls"})
    assert claude_tool("grep", {"pattern": "zone", "path": "/r"}) == ("Grep", {"pattern": "zone", "path": "/r"})
    assert claude_tool("glob", {"pattern": "*.md"}) == ("Glob", {"pattern": "*.md"})
    assert claude_tool("webfetch", {"url": "https://x"}) == ("WebFetch", {"url": "https://x"})
    assert claude_tool("websearch_web_search_exa", {"query": "q", "objective": "o"}) == ("WebSearch", {"query": "q"})
    assert claude_tool("skill", {"id": "sediment-framework"}) == ("Skill", {"skill": "sediment-framework"})
    task = {"description": "d", "agent": "fixer", "prompt": "p"}
    assert claude_tool("subagent", task) == ("Agent", {"description": "d", "subagent_type": "fixer", "prompt": "p"})
    assert claude_tool("wisburg_list-feed", {"n": 1}) == ("wisburg_list-feed", {"n": 1})


def test_mcp_servers_empty_without_file(tmp_path):
    assert mcp_servers(tmp_path) == {}


def test_mcp_servers_in_v2_shape(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "wisburg": {"type": "http", "url": "https://w/mcp", "headers": {"Authorization": "Bearer k"}},
        "local": {"command": "npx", "args": ["srv"], "env": {"TOKEN": "t"}},
    }}), encoding="utf-8")
    assert mcp_servers(tmp_path) == {
        "wisburg": {"type": "remote", "url": "https://w/mcp", "headers": {"Authorization": "Bearer k"}},
        "local": {"type": "local", "command": ["npx", "srv"], "environment": {"TOKEN": "t"}},
    }
