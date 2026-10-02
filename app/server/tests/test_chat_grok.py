from __future__ import annotations

import subprocess

from lynch_reader.chat.grok import (DENY_RULES, ISOLATION_ENV, USER_HEADING, GrokConfig, discover_prompt, grok_argv,
                                    grok_env, grok_models, prepare_workdir)
from tests.chat_helpers import conv, write_models_cache


def cfg(tmp_path):
    return GrokConfig(("grok",), tmp_path / "wd", tmp_path / "home", tmp_path / "logs", tmp_path / "invest",
                      tmp_path / "search.md")


def discover_conv():
    return conv(kind="discover", ticker=None, model="grok-4.6", effort="high", context_window=500000,
                grok_session="g-1")


def test_argv_resumes_the_session_with_model_and_effort(tmp_path):
    argv = grok_argv(cfg(tmp_path), discover_conv(), "PROMPT")
    assert argv[0] == "grok" and "--single=PROMPT" in argv
    assert argv[argv.index("--resume") + 1] == "g-1"
    assert argv[argv.index("--model") + 1] == "grok-4.6"
    assert argv[argv.index("--reasoning-effort") + 1] == "high"
    assert "--rules" not in argv  # grok drops it on a resumed ACP session: the skill travels in the message
    assert argv[argv.index("--output-format") + 1] == "streaming-messages-json"
    assert "--include-partial-messages" in argv


def test_a_message_starting_with_a_dash_stays_the_prompt(tmp_path):
    argv = grok_argv(cfg(tmp_path), discover_conv(), "- 只看 alice")
    assert "--single=- 只看 alice" in argv and "- 只看 alice" not in argv


def test_argv_isolates_grok_to_search_only(tmp_path):
    argv = grok_argv(cfg(tmp_path), discover_conv(), "x")
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


def test_prompt_is_skill_then_sources_then_app_context_then_user_words():
    p = discover_prompt("# SKILL\nrules", "- 账号：@alice", "看看")
    assert p.index("# SKILL") < p.index("## 本次的 KOL 来源") < p.index("- 账号：@alice") \
        < p.index("## 运行环境") < p.index(USER_HEADING) < p.index("看看")
    assert "没有文件和终端工具" in p and "简体中文" in p
    assert p.endswith(f"{USER_HEADING}\n看看")


def test_workdir_is_its_own_git_root(tmp_path):
    wd = tmp_path / "wd"
    prepare_workdir(wd)
    prepare_workdir(wd)  # idempotent
    top = subprocess.run(["git", "-C", str(wd), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    assert top.stdout.strip() == str(wd.resolve())


import asyncio
import os

import pytest

from lynch_reader.chat.grok import GrokSetupError, new_session
from tests.chat_helpers import fake_grok_env, grok_config, grok_launches, grok_session_file


def setup_env(tmp_path, **extra):
    return grok_env({**os.environ, **fake_grok_env(tmp_path), **extra})


def test_new_session_sets_model_and_window_over_acp(tmp_path):
    cfg = grok_config(tmp_path)
    sid = asyncio.run(new_session(cfg, setup_env(tmp_path), "grok-4.6", 500000))
    assert grok_session_file(tmp_path, sid) == {"model": "grok-4.6", "window": 500000, "cwd": str(cfg.workdir)}
    [launch] = grok_launches(tmp_path)
    assert launch["argv"] == ["agent", "stdio"] and launch["cwd"] == str(cfg.workdir)
    assert launch["grok_env"]["GROK_SANDBOX"] == "read-only"  # the same profile every later turn resumes under
    assert (cfg.workdir / ".git").is_dir()


def test_new_session_reports_an_acp_error(tmp_path):
    env = setup_env(tmp_path, FAKE_GROK_ACP_FAIL="session/set_model")
    with pytest.raises(GrokSetupError, match="session/set_model"):
        asyncio.run(new_session(grok_config(tmp_path), env, "grok-4.6", 500000))


def test_new_session_reports_grok_exiting(tmp_path):
    env = setup_env(tmp_path, FAKE_GROK_ACP_EXIT="1")
    with pytest.raises(GrokSetupError, match="not logged in"):
        asyncio.run(new_session(grok_config(tmp_path), env, "grok-4.6", 256000))


def test_new_session_times_out(tmp_path):
    env = setup_env(tmp_path, FAKE_GROK_ACP_HANG="1")
    with pytest.raises(GrokSetupError, match="没有响应"):
        asyncio.run(new_session(grok_config(tmp_path), env, "grok-4.6", 256000, timeout_s=0.5))
