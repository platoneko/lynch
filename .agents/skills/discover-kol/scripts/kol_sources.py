"""Prints the KOL sources a discover search covers: invest's X list, handles and lookback (`just kol-sources`)."""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from lynch_reader.chat.kol import INVEST_ROOT, KolUnavailable, kol_brief, kol_sources


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--invest", type=Path, default=INVEST_ROOT, help="invest checkout (default: next to lynch)")
    args = ap.parse_args(argv)
    try:
        kol = asyncio.run(kol_sources(args.invest))
    except KolUnavailable as e:
        print(e, file=sys.stderr)
        return 1
    print(kol_brief(kol, datetime.now(UTC)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
