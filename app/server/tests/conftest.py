"""A fake frameworks/ tree shaped like the real repo, and a fake Longbridge quote batch."""
from __future__ import annotations

from pathlib import Path

import pytest

from lynch_reader.catalog import Quote, QuoteCache

NOW = 1_790_000_000.0  # fake clock, seconds since the epoch


def framework_md(ticker: str, name: str, lynch: str, blocked: str | None = None) -> str:
    lines = [
        f"# {ticker} — {name}",
        "",
        f"> 🟡 林奇：{lynch} | 更新：2026-09-21 | eval: eval/{ticker}.py",
        "> 主尺：test ruler",
    ]
    if blocked:
        lines.append(f"> **状态：BLOCKED**（{blocked}）")
    lines += ["", "## 0. 现行结论", "", "- **价带锚点**（USD）：L = **10** · M = **20** · H = **40**", ""]
    return "\n".join(lines)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    fw = tmp_path / "frameworks"
    fw.mkdir()
    (fw / "AAA.US.md").write_text(framework_md("AAA.US", "Alpha Corp", "`fast_grower` / `cyclical`"), encoding="utf-8")
    (fw / "BBB.US.md").write_text(
        framework_md("BBB.US", "Beta Inc", "`stalwart`", blocked="test reason; unblock when X"), encoding="utf-8"
    )
    (fw / "0001.HK.md").write_text("# 0001.HK — Broken\n\nno anchors here\n", encoding="utf-8")
    (fw / "_TEMPLATE.md").write_text("# template\n", encoding="utf-8")
    return fw


class FakeFetch:
    """Stands in for `fetch_quotes`: fixed last prices, a call log, and an optional failure."""

    def __init__(self, prices: dict[str, float], error: Exception | None = None) -> None:
        self.prices = prices
        self.error = error
        self.calls: list[list[str]] = []

    def __call__(self, symbols: list[str]) -> dict[str, Quote]:
        self.calls.append(list(symbols))
        if self.error is not None:
            raise self.error
        return {s: Quote(symbol=s, last=p, prev_close=None, status="Normal")
                for s, p in self.prices.items() if s in symbols}


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fetch() -> FakeFetch:
    return FakeFetch({"AAA.US": 25.0, "BBB.US": 5.0})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def quotes(fetch: FakeFetch, clock: FakeClock) -> QuoteCache:
    # One fake serves as both clocks unless a test needs them apart.
    return QuoteCache(fetch=fetch, monotonic=clock, wall=clock)
