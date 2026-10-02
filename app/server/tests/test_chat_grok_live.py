"""Real grok, opt-in: LYNCH_LIVE_GROK=1 uv run pytest tests/test_chat_grok_live.py -q -s

Guards what spec §1 found by experiment and grok does not document: X search survives the isolation while the
terminal and files do not, and the ACP-made session keeps a 500k window. Run it after every grok upgrade. Needs a
logged-in `grok` (grok.com) and ../invest with a KOL list. Spends a few cents of Grok usage; leaves session
directories under ~/.grok/sessions/ for the pytest tmp paths.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import urllib.parse
from pathlib import Path

import pytest

from lynch_reader.catalog import REPO_ROOT
from lynch_reader.chat.grok import GrokConfig, discover_rules, grok_argv, grok_env, grok_models, new_session
from lynch_reader.chat.kol import KolSources, kol_sources
from tests.chat_helpers import conv

pytestmark = pytest.mark.skipif(os.environ.get("LYNCH_LIVE_GROK") != "1", reason="real grok, opt-in")

MODEL, EFFORT = "grok-4.7-build-fast", "low"
TERMINAL_OR_FILES = {"run_terminal_cmd", "run_terminal_command", "read_file", "write", "search_replace", "list_dir",
                     "grep", "spawn_subagent"}


def config(tmp_path: Path) -> GrokConfig:
    # frameworks/ (a real dir, or the symlink a worktree needs) always lives in the main lynch checkout, next to invest
    invest = (REPO_ROOT / "frameworks").resolve().parent.parent / "invest"
    return GrokConfig((shutil.which("grok") or "grok",), tmp_path / "wd", Path.home(), tmp_path / "logs", invest)


async def run_turn(cfg: GrokConfig, text: str, rules: str, window: int = 256000) -> list[dict]:
    env = grok_env(os.environ)
    session = await new_session(cfg, env, MODEL, window)
    argv = grok_argv(cfg, conv(kind="discover", ticker=None, model=MODEL, effort=EFFORT, grok_session=session),
                     text, rules)
    proc = await asyncio.create_subprocess_exec(*argv, cwd=cfg.workdir, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024)
    out, _ = await asyncio.wait_for(proc.communicate(), 600)
    return [json.loads(l) for l in out.splitlines() if l.strip().startswith(b"{")]


def tool_uses(lines: list[dict]) -> list[dict]:
    return [b for l in lines if l["type"] == "assistant" for b in l["message"]["content"] if b["type"] == "tool_use"]


def test_isolation_keeps_x_search_and_blocks_the_rest(tmp_path):
    if MODEL not in {m.id for m in grok_models(Path.home())}:
        pytest.skip(f"{MODEL} not in grok's model cache")
    rules = discover_rules(KolSources(None, ("elonmusk",), 36))
    lines = asyncio.run(run_turn(config(tmp_path),
                                 "Run the shell command `echo hi` and read the file /etc/hostname. Then search X "
                                 "for the 1 latest post from:elonmusk and give its id. Do not install anything.",
                                 rules))
    [init] = [l for l in lines if l["type"] == "system" and l.get("subtype") == "init"]
    assert not TERMINAL_OR_FILES & set(init["tools"]), init["tools"]
    assert any(u["name"].startswith("X search") for u in tool_uses(lines))
    results = [b for l in lines if l["type"] == "user" for b in l["message"]["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert not any("exit: 0\nhi" in str(r.get("content")) for r in results)
    assert lines[-1]["type"] == "result" and lines[-1]["subtype"] == "success"


def test_acp_session_keeps_a_500k_window(tmp_path):
    cfg = config(tmp_path)
    session = asyncio.run(new_session(cfg, grok_env(os.environ), MODEL, 500000))
    summary = (Path.home() / ".grok" / "sessions" / urllib.parse.quote(str(cfg.workdir), safe="") / session
               / "summary.json").read_text()
    assert re.search(r'"context_window":\s*500000', summary), summary


@pytest.mark.skipif(os.environ.get("LYNCH_LIVE_GROK_KOL") != "1", reason="full KOL search, opt-in")
def test_full_kol_turn_fits_the_turn_cap(tmp_path):
    cfg = config(tmp_path)
    rules = discover_rules(asyncio.run(kol_sources(cfg.invest_root)))
    lines = asyncio.run(run_turn(cfg, "看看名单里 KOL 最近提到了哪些票", rules))
    result = lines[-1]
    print("num_turns:", result.get("num_turns"), "cost:", result.get("total_cost_usd"))
    assert result["type"] == "result" and result["subtype"] == "success", result
