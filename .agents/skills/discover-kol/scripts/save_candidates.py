"""Saves the tickers the user named from a discover reply into lynch's candidate list (`just candidates-save`).

Only tickers in the reply's lynch-candidates block can be saved; if any named ticker is not there, nothing is.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lynch_reader.chat.candidates import CANDIDATES_FILE, CandidateStore, parse_candidates

SOURCE = "terminal"  # recorded on each saved source where the app records its chat id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("reply", type=Path, help="the reply, candidate block included")
    ap.add_argument("tickers", nargs="+", help="tickers the user chose, e.g. AXTI.US 0700.HK")
    ap.add_argument("--store", type=Path, default=CANDIDATES_FILE)
    args = ap.parse_args(argv)

    try:
        text = args.reply.read_text(encoding="utf-8")
    except OSError as e:
        print(f"读不到回答文件：{e}", file=sys.stderr)
        return 1
    parsed = parse_candidates(text)
    if parsed is None:
        print("文件里没有可用的 lynch-candidates 候选块", file=sys.stderr)
        return 1
    if parsed.dropped:
        print(f"注意：候选块里有 {parsed.dropped} 条候选或出处格式不对，已略过", file=sys.stderr)

    in_block = {c["ticker"]: c for c in parsed.candidates}
    named = list(dict.fromkeys(t.strip().upper() for t in args.tickers))
    missing = [t for t in named if t not in in_block]
    if missing:
        print("这些票不在候选块里，没有写入：" + "、".join(missing), file=sys.stderr)
        return 1
    saved = CandidateStore(args.store).save(SOURCE, [in_block[t] for t in named])
    print("已存入候选区：" + "、".join(saved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
