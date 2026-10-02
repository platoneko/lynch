from __future__ import annotations

import subprocess

from lynch_reader.chat.grok import (DENY_RULES, ISOLATION_ENV, GrokConfig, discover_rules, grok_argv, grok_env,
                                    grok_models, prepare_workdir)
from lynch_reader.chat.kol import KolSources
from tests.chat_helpers import conv, write_models_cache


def cfg(tmp_path):
    return GrokConfig(("grok",), tmp_path / "wd", tmp_path / "home", tmp_path / "logs", tmp_path / "invest")


def discover_conv():
    return conv(kind="discover", ticker=None, model="grok-4.6", effort="high", context_window=500000,
                grok_session="g-1")


def test_argv_resumes_the_session_with_model_effort_and_rules(tmp_path):
    argv = grok_argv(cfg(tmp_path), discover_conv(), "看看", "RULES")
    assert argv[0] == "grok" and "--single=看看" in argv
    assert argv[argv.index("--resume") + 1] == "g-1"
    assert argv[argv.index("--model") + 1] == "grok-4.6"
    assert argv[argv.index("--reasoning-effort") + 1] == "high"
    assert argv[argv.index("--rules") + 1] == "RULES"
    assert argv[argv.index("--output-format") + 1] == "streaming-messages-json"
    assert "--include-partial-messages" in argv


def test_a_message_starting_with_a_dash_stays_the_prompt(tmp_path):
    argv = grok_argv(cfg(tmp_path), discover_conv(), "- 只看 alice", "R")
    assert "--single=- 只看 alice" in argv and "- 只看 alice" not in argv


def test_argv_isolates_grok_to_search_only(tmp_path):
    argv = grok_argv(cfg(tmp_path), discover_conv(), "x", "R")
    denied = [argv[i + 1] for i, a in enumerate(argv) if a == "--deny"]
    assert denied == list(DENY_RULES) and "MCPTool(*)" in denied and "Bash" in denied
    off = argv[argv.index("--disallowed-tools") + 1].split(",")
    assert {"run_terminal_cmd", "run_terminal_command", "write", "read_file", "spawn_subagent"} <= set(off)
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--no-subagents" in argv and "--no-auto-update" in argv
    assert "--tools" not in argv  # one unknown name there and grok falls back to every tool


def test_env_adds_isolation_and_keeps_the_rest():
    env = grok_env({"PATH": "/bin", "HOME": "/h"})
    assert env["PATH"] == "/bin" and env["GROK_SANDBOX"] == "read-only"
    assert env["GROK_CURSOR_MCPS_ENABLED"] == "0" and env["GROK_CLAUDE_AGENTS_ENABLED"] == "0"
    assert ISOLATION_ENV.items() <= env.items()


def test_models_keep_visible_search_capable_ones(tmp_path):
    write_models_cache(tmp_path)
    [m] = grok_models(tmp_path)
    assert m.public() == {"id": "grok-4.6", "label": "Grok 4.6", "windows": [256000, 500000],
                          "efforts": ["high", "medium", "low"], "default_effort": "high"}


def test_models_are_empty_without_a_readable_cache(tmp_path):
    assert grok_models(tmp_path) == []
    (tmp_path / ".grok").mkdir()
    (tmp_path / ".grok" / "models_cache.json").write_text("{not json", encoding="utf-8")
    assert grok_models(tmp_path) == []


def test_rules_name_the_sources_window_and_block():
    rules = discover_rules(KolSources("123", ("alice", "bob"), 48))
    assert "x.com/i/lists/123" in rules and "@alice" in rules and "@bob" in rules and "48 小时" in rules
    assert "```lynch-candidates" in rules and "中文简称" in rules
    assert "x.com/i/lists" not in discover_rules(KolSources(None, ("alice",), 36))


def test_workdir_is_its_own_git_root(tmp_path):
    wd = tmp_path / "wd"
    prepare_workdir(wd)
    prepare_workdir(wd)  # idempotent
    top = subprocess.run(["git", "-C", str(wd), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    assert top.stdout.strip() == str(wd.resolve())
