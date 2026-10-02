from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from lynch_reader.catalog import REPO_ROOT
from tests.chat_helpers import write_invest

SCRIPTS = REPO_ROOT / ".agents" / "skills" / "discover-kol" / "scripts"
SERVER = REPO_ROOT / "app" / "server"
REPLY = """两只票。

```lynch-candidates
{"candidates": [
 {"ticker": "0700.HK", "name": "腾讯控股", "sources": [{"handle": "alice", "stance": "bullish", "reason": "r",
  "quote": "q", "url": "https://x.com/alice/status/1", "posted_at": "2026-10-01T08:00:00Z"}]},
 {"ticker": "AXTI.US", "name": "AXT Inc", "sources": [{"handle": "bob", "stance": "bearish", "reason": "r",
  "quote": "q", "url": "https://x.com/bob/status/2", "posted_at": "2026-10-01T09:00:00Z"}]}
]}
```"""


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SERVER)}
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True, env=env,
                          timeout=30)


def test_kol_sources_prints_the_brief(tmp_path):
    invest = write_invest(tmp_path / "invest", list_id="123", handles=("alice",), hours=48)
    p = run("kol_sources.py", "--invest", str(invest))
    assert p.returncode == 0, p.stderr
    assert "list:123" in p.stdout and "@alice" in p.stdout and "最近 48 小时" in p.stdout


def test_kol_sources_fails_on_an_empty_list(tmp_path):
    invest = write_invest(tmp_path / "invest", list_id=None, handles=())
    p = run("kol_sources.py", "--invest", str(invest))
    assert p.returncode == 1 and "为空" in p.stderr and p.stdout == ""


def save(tmp_path: Path, text: str, *tickers: str) -> subprocess.CompletedProcess:
    reply = tmp_path / "reply.md"
    reply.write_text(text, encoding="utf-8")
    return run("save_candidates.py", str(reply), *tickers, "--store", str(tmp_path / "candidates.json"))


def stored(tmp_path: Path) -> list[dict]:
    path = tmp_path / "candidates.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def test_saves_only_the_named_tickers(tmp_path):
    p = save(tmp_path, REPLY, "0700.HK")
    assert p.returncode == 0, p.stderr
    assert "0700.HK" in p.stdout
    [row] = stored(tmp_path)
    assert row["ticker"] == "0700.HK" and row["sources"][0]["conv_id"] == "terminal"


def test_lower_case_tickers_are_accepted(tmp_path):
    assert save(tmp_path, REPLY, "axti.us").returncode == 0
    assert [r["ticker"] for r in stored(tmp_path)] == ["AXTI.US"]


def test_a_ticker_not_in_the_block_writes_nothing(tmp_path):
    p = save(tmp_path, REPLY, "0700.HK", "NVDA.US")
    assert p.returncode == 1 and "NVDA.US" in p.stderr and stored(tmp_path) == []


def test_a_reply_without_a_block_is_refused(tmp_path):
    p = save(tmp_path, "没有候选。", "0700.HK")
    assert p.returncode == 1 and "候选块" in p.stderr


def test_an_unreadable_reply_file_is_refused(tmp_path):
    p = run("save_candidates.py", str(tmp_path / "missing.md"), "0700.HK", "--store", str(tmp_path / "c.json"))
    assert p.returncode == 1 and "读不到" in p.stderr
