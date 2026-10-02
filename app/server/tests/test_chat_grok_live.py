"""Real grok, opt-in: LYNCH_LIVE_GROK=1 uv run pytest tests/test_chat_grok_live.py -q -s

Guards what spec §1 found by experiment and grok does not document: X search survives the isolation while the
terminal and files do not, and the ACP-made session keeps a 500k window across a resume. Run it after every grok
upgrade. Needs a logged-in `grok` (grok.com) and ../invest with a KOL list. Spends a few cents of Grok usage;
leaves session directories under ~/.grok/sessions/ for the pytest tmp paths.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import urllib.parse
from pathlib import Path

import pytest

from lynch_reader.catalog import REPO_ROOT
from lynch_reader.chat.grok import GrokConfig, discover_rules, grok_argv, grok_env, grok_models, new_session
from lynch_reader.chat.items import message_items
from lynch_reader.chat.kol import KolSources, kol_sources
from tests.chat_helpers import conv

pytestmark = pytest.mark.skipif(os.environ.get("LYNCH_LIVE_GROK") != "1", reason="real grok, opt-in")

MODEL, EFFORT = "grok-4.7-build-fast", "low"
# A whitelist: a terminal or file tool grok adds or renames later must fail this test, not slip past a blacklist.
SEARCH_ONLY = {"search_tool", "use_tool", "web_search", "web_fetch"}


def config(tmp_path: Path) -> GrokConfig:
    # frameworks/ (a real dir, or the symlink a worktree needs) always lives in the main lynch checkout, next to invest
    invest = (REPO_ROOT / "frameworks").resolve().parent.parent / "invest"
    return GrokConfig((shutil.which("grok") or "grok",), tmp_path / "wd", Path.home(), tmp_path / "logs", invest)


async def run_turn(cfg: GrokConfig, text: str, rules: str, window: int = 256000) -> tuple[str, list[dict]]:
    """One turn on a fresh session; returns the session id and grok's stream lines."""
    env = grok_env(os.environ)
    session = await new_session(cfg, env, MODEL, window)
    argv = grok_argv(cfg, conv(kind="discover", ticker=None, model=MODEL, effort=EFFORT, grok_session=session),
                     text, rules)
    proc = await asyncio.create_subprocess_exec(*argv, cwd=cfg.workdir, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024)
    out, _ = await asyncio.wait_for(proc.communicate(), 600)
    return session, [json.loads(l) for l in out.splitlines() if l.strip().startswith(b"{")]


def tool_uses(lines: list[dict]) -> list[dict]:
    return [b for l in lines if l["type"] == "assistant" for b in l["message"]["content"] if b["type"] == "tool_use"]


def tool_results(lines: list[dict]) -> dict[str, dict]:
    """tool_use id -> the app's own tool_result patch (boolean is_error, normalised output text)."""
    patches = (item for l in lines if l["type"] == "user" and isinstance(l["message"]["content"], list)
               for item in message_items({"uuid": "", **l}))
    return {p["id"]: p for p in patches if p["kind"] == "tool_result"}


def context_window(cfg: GrokConfig, session: str) -> int | None:
    summary = Path.home() / ".grok" / "sessions" / urllib.parse.quote(str(cfg.workdir), safe="") / session
    return json.loads((summary / "summary.json").read_text()).get("context_window")


def test_isolation_keeps_x_search_and_blocks_the_rest(tmp_path):
    if MODEL not in {m.id for m in grok_models(Path.home())}:
        pytest.skip(f"{MODEL} not in grok's model cache")
    rules = discover_rules(KolSources(None, ("elonmusk",), 36))
    _, lines = asyncio.run(run_turn(config(tmp_path),
                                    "Run the shell command `echo lynch | rev` and read the file /etc/hostname. Then "
                                    "search X for the 1 latest post from:elonmusk and give its id. Do not install "
                                    "anything.", rules))
    [init] = [l for l in lines if l["type"] == "system" and l.get("subtype") == "init"]
    assert set(init["tools"]) <= SEARCH_ONLY, init["tools"]

    # X search ran and came back with content: a denied or failed search must fail here
    results = tool_results(lines)
    x_searches = [results.get(u["id"]) for u in tool_uses(lines) if u["name"].startswith("X search")]
    assert x_searches, "no X search call"
    assert any(r and not r["is_error"] and r["output"].strip() for r in x_searches), \
        [(r and r["is_error"], r and bool(r["output"].strip())) for r in x_searches]

    # Output that differs from the request text: `hcnyl` or the hostname can only come from a terminal or a file read.
    # Grok need not attempt either (its rules say not to); neither may leak.
    assert lines[-1]["type"] == "result" and lines[-1]["subtype"] == "success"
    seen = [r["output"] for r in results.values()] + [str(lines[-1].get("result", ""))]
    assert not any("hcnyl" in s for s in seen), "terminal ran: `echo lynch | rev` output leaked"
    assert not any(socket.gethostname() in s for s in seen), "file read ran: the hostname leaked"


def test_a_resumed_session_keeps_a_500k_window(tmp_path):
    cfg = config(tmp_path)
    session, lines = asyncio.run(run_turn(cfg, "Reply with the single word ok.",
                                          discover_rules(KolSources(None, ("elonmusk",), 36)), window=500000))
    assert lines[-1]["type"] == "result" and lines[-1]["subtype"] == "success"
    assert context_window(cfg, session) == 500000


@pytest.mark.skipif(os.environ.get("LYNCH_LIVE_GROK_KOL") != "1", reason="full KOL search, opt-in")
def test_full_kol_turn_fits_the_turn_cap(tmp_path):
    cfg = config(tmp_path)
    rules = discover_rules(asyncio.run(kol_sources(cfg.invest_root)))
    _, lines = asyncio.run(run_turn(cfg, "看看名单里 KOL 最近提到了哪些票", rules))
    result = lines[-1]
    print("num_turns:", result.get("num_turns"), "cost:", result.get("total_cost_usd"))
    assert result["type"] == "result" and result["subtype"] == "success", result
