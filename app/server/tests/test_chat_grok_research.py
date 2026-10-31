from __future__ import annotations

from lynch_reader.chat.config import GIT_BLOCKED_COMMANDS, LONGBRIDGE_BLOCKED_COMMANDS
from lynch_reader.chat.grok import (DEFAULT_RESEARCH_EFFORT, DEFAULT_RESEARCH_MODEL, RESEARCH_ASK, RESEARCH_ALLOW,
                                    RESEARCH_DENY, grok_models, grok_tool, research_argv, research_env,
                                    research_prompt, write_research_home, write_research_rules)
from tests.chat_helpers import conv, write_models_cache
from tests.test_chat_grok import cfg


def test_research_models_include_ones_that_cannot_search_x(tmp_path):
    write_models_cache(tmp_path)
    ids = [m.id for m in grok_models(tmp_path, search_only=False)]
    assert ids == ["grok-4.6", "grok-plain"]  # hidden grok-secret omitted; grok-plain has no X search


def test_discover_models_still_require_x_search(tmp_path):
    write_models_cache(tmp_path)
    assert [m.id for m in grok_models(tmp_path)] == ["grok-4.6"]


def test_research_env_does_not_use_discover_isolation():
    env = research_env({"PATH": "/bin"})
    assert env["PATH"] == "/bin"
    assert env["GROK_CURSOR_MCPS_ENABLED"] == "0"
    assert env["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert env["GROK_MEMORY"] == "0"
    assert "GROK_SANDBOX" not in env
    assert env.get("GROK_CLAUDE_SKILLS_ENABLED") != "0"


def test_research_argv_is_agent_stdio_not_single(tmp_path):
    argv = research_argv(cfg(tmp_path))
    assert argv[-2:] == ["agent", "stdio"]
    assert not any(a.startswith("--single") for a in argv)
    # spike §2: grok agent stdio ignores top-level --permission-mode/--allow/--deny; rules live in GROK_HOME.
    assert "--permission-mode" not in argv
    assert "--deny" not in argv and "--allow" not in argv


def test_research_defaults_are_four_six_and_high():
    assert DEFAULT_RESEARCH_MODEL == "grok-4.6" and DEFAULT_RESEARCH_EFFORT == "high"


def test_research_prompt_is_scope_plus_shared_rules():
    p = research_prompt(conv(kind="ticker", ticker="AAA.US"))
    assert "frameworks/AAA.US.md" in p and "简体中文" in p
    assert "sediment-framework" in p and "lynch-watch" in p
    assert "frameworks/" in p and "MCP" in p


def test_write_research_rules_is_the_prompt(tmp_path):
    c = conv(kind="ticker", ticker="AAA.US")
    path = write_research_rules(cfg(tmp_path), c)
    assert path == tmp_path / "logs" / "c1.rules.md"
    assert path.read_text(encoding="utf-8") == research_prompt(c)


def test_grok_tool_maps_write_and_edit_to_claude_shape():
    name, inp = grok_tool("write", {"file_path": "frameworks/ZZZ.US.md", "content": "x"})
    assert name == "Write" and inp["file_path"].endswith("ZZZ.US.md") and inp["content"] == "x"
    name, inp = grok_tool("search_replace", {
        "file_path": "frameworks/ZZZ.US.md", "old_string": "a", "new_string": "b", "replace_all": False})
    assert name == "Edit" and inp["old_string"] == "a" and inp["new_string"] == "b"
    name, inp = grok_tool("read_file", {"target_file": "frameworks/ZZZ.US.md", "offset": 1})
    assert name == "Read" and inp["file_path"].endswith("ZZZ.US.md") and inp["offset"] == 1
    name, inp = grok_tool("run_terminal_command", {"command": "echo hi", "description": "x"})
    assert name == "Bash" and inp["command"] == "echo hi"
    name, inp = grok_tool("spawn_subagent", {"description": "d", "prompt": "p"})
    assert name == "Agent" and inp["prompt"] == "p"
    assert grok_tool("mystery", {"a": 1}) == ("mystery", {"a": 1})


def test_research_permission_lists_allow_frameworks_ask_skill_deny_eval():
    assert "Edit(frameworks/**)" in RESEARCH_ALLOW and "Write(frameworks/**)" in RESEARCH_ALLOW
    assert "Bash(*)" in RESEARCH_ALLOW
    assert "WebSearch" in RESEARCH_ALLOW and "WebFetch" in RESEARCH_ALLOW
    assert any("sediment-framework" in r for r in RESEARCH_ASK)
    assert "Write(**/eval/**)" in RESEARCH_DENY and "Edit(**/eval/**)" in RESEARCH_DENY
    assert "Bash(sudo *)" in RESEARCH_DENY
    assert all(f"Bash(git *{c}*)" in RESEARCH_DENY for c in GIT_BLOCKED_COMMANDS)
    assert all(f"Bash(longbridge {c}*)" in RESEARCH_DENY for c in LONGBRIDGE_BLOCKED_COMMANDS)
    assert "MCPTool(longbridge__*)" in RESEARCH_DENY


def test_write_research_home_is_dontAsk_with_symlinked_auth(tmp_path):
    real = tmp_path / "real-grok"
    real.mkdir()
    (real / "auth.json").write_text("token", encoding="utf-8")
    (real / "models_cache.json").write_text("{}", encoding="utf-8")
    home = write_research_home(cfg(tmp_path), conv(kind="research", ticker=None), real)
    assert home == tmp_path / "logs" / "c1.grok-home"
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert 'permission_mode = "dontAsk"' in text
    assert "Edit(frameworks/**)" in text and "sediment-framework" in text
    assert (home / "auth.json").resolve() == (real / "auth.json").resolve()
