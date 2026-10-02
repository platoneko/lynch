"""Prints what a discover search covers: invest's X List and the lookback window (`just kol-sources`)."""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from lynch_reader.chat.kol import DEFAULT_LOOKBACK_DAYS, INVEST_ROOT, LOOKBACK_DAYS, KolUnavailable, kol_brief, kol_sources


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--invest", type=Path, default=INVEST_ROOT, help="invest checkout (default: next to lynch)")
    ap.add_argument("--days", type=int, choices=LOOKBACK_DAYS, default=DEFAULT_LOOKBACK_DAYS, help="days to look back")
    args = ap.parse_args(argv)
    try:
        kol = asyncio.run(kol_sources(args.invest))
    except KolUnavailable as e:
        print(e, file=sys.stderr)
        return 1
    print(kol_brief(kol, args.days, datetime.now(UTC)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
