#!/usr/bin/env python3
"""Feishu notify when framework-universe earnings are actually available.

Pools:
  --market ha  → .HK/.SH/.SZ, BJT calendar day
  --market us  → .US, NYT calendar day

Dual confirm: earn date in [T+0 .. T+6] AND latest report period advanced.
Push: regular T+0/T+1 every run; extend first-confirm once.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.earnings_available import (  # noqa: E402
    EXTEND_MAX_D,
    MarketPool,
    UniverseItem,
    evaluate,
    filter_universe,
    format_message,
    market_tz,
    pool_of,
    state_from_jsonable,
    state_to_jsonable,
    today_in_tz,
)
from eval.lib.earnings_lb import fetch_earn_dates_in_window, fetch_latest_periods  # noqa: E402
from eval.lib.feishu import send_post  # noqa: E402
from eval.lib.frameworks import _TITLE, list_framework_paths  # noqa: E402

log = logging.getLogger("notify_earnings_available")

DEFAULT_STATE = REPO_ROOT / "_local" / "earnings_available_state.json"


def load_universe() -> list[UniverseItem]:
    items: list[UniverseItem] = []
    for path in list_framework_paths():
        md = path.read_text(encoding="utf-8")
        m = _TITLE.search(md)
        if not m:
            log.warning("skip %s: missing title", path.name)
            continue
        ticker, name = m.group(1), m.group(2).strip()
        # Prefer short CN name before " / ".
        if " / " in name:
            name = name.split(" / ", 1)[0].strip()
        try:
            pool_of(ticker)
        except ValueError:
            log.warning("skip %s: unsupported suffix", ticker)
            continue
        items.append(UniverseItem(ticker=ticker, name=name))
    return items


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("state read failed %s: %s — starting empty", path, e)
        return {}
    if not isinstance(raw, dict):
        return {}
    tickers = raw.get("tickers")
    if isinstance(tickers, dict):
        return state_from_jsonable(tickers)
    # Allow bare {ticker: {...}} for hand edits.
    return state_from_jsonable(raw)


def save_state(path: Path, state: dict, *, dry_run: bool) -> None:
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tickers": state_to_jsonable(state),
    }
    if dry_run:
        log.info("dry_run skip state write → %s (%d tickers)", path, len(state))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--market",
        choices=("ha", "us"),
        required=True,
        help="ha = H/A (BJT evening job); us = US (NYT after-close / BJT morning job)",
    )
    ap.add_argument("--dry-run", action="store_true", help="No Feishu HTTP; no state write")
    ap.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Override market-local today for every ticker in this pool",
    )
    ap.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE,
        help=f"State JSON (default {DEFAULT_STATE})",
    )
    ap.add_argument("--no-notify", action="store_true", help="Evaluate + write state, skip Feishu")
    args = ap.parse_args(argv)

    pool: MarketPool = args.market
    override = date.fromisoformat(args.date) if args.date else None

    universe = filter_universe(load_universe(), pool)
    if not universe:
        log.info("empty universe for market=%s", pool)
        return 0

    # One market-local "today" for the whole pool (all HA share BJT; all US share NYT).
    sample_tz = market_tz(universe[0].ticker)
    today = today_in_tz(sample_tz, override)
    today_by = {it.ticker: today for it in universe}
    tickers = [it.ticker for it in universe]

    log.info(
        "market=%s today=%s (%s) universe=%d",
        pool,
        today.isoformat(),
        sample_tz.key,
        len(universe),
    )

    earn_dates = fetch_earn_dates_in_window(tickers, today=today, max_d=EXTEND_MAX_D)
    log.info("earn_dates in window: %d → %s", len(earn_dates), sorted(earn_dates))

    # Fetch latest for the whole pool so out-of-window tickers get a baseline
    # seed; otherwise the first day inside a window would only bootstrap and
    # never notify.
    state = load_state(args.state)
    latest = fetch_latest_periods(tickers)
    log.info("latest periods fetched: %d / %d", len(latest), len(tickers))

    result = evaluate(
        universe,
        earn_dates=earn_dates,
        latest_periods=latest,
        state=state,
        today_by_ticker=today_by,
    )

    # Preserve state for tickers in other pool / not in this run.
    merged = dict(state)
    merged.update(result.state)
    # Drop evaluate copies for tickers not in this pool run? Keep all — harmless.
    save_state(args.state, merged, dry_run=args.dry_run)

    for line in result.skipped:
        log.debug("skip %s", line)
    log.info("hits=%d skipped=%d", len(result.hits), len(result.skipped))

    if not result.hits:
        return 0

    title, lines = format_message(result.hits, as_of=today, pool=pool)
    for ln in lines:
        log.info("hit: %s", ln.strip())

    if args.no_notify:
        log.info("no-notify: skip Feishu")
        return 0

    feishu_result = send_post(title, lines, at_all=False, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    if feishu_result.get("skipped_reason") == "missing_webhook":
        log.error("Feishu webhook missing")
        return 2
    if not feishu_result.get("ok"):
        log.error("Feishu send failed: %s", feishu_result)
        return 1
    log.info("feishu ok title=%s", title)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
