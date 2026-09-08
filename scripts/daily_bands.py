#!/usr/bin/env python3
"""Daily: quote → zone from framework L/M/H → write bands → Feishu notify.

Always sends a daily digest. If any ticker's zone changed vs previous artifact
(and previous_zone was not null), the message @所有人.

Does NOT write frameworks/*.md.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.bands_io import build_record, read_previous, write_bands  # noqa: E402
from eval.lib.feishu import send_post  # noqa: E402
from eval.lib.frameworks import FrameworkMeta, load_all_frameworks  # noqa: E402
from eval.lib.price import fetch_quotes  # noqa: E402
from eval.lib.zones import zone_emoji, zone_label, zone_of  # noqa: E402

log = logging.getLogger("daily_bands")
TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class Row:
    meta: FrameworkMeta
    price: float
    zone: str
    previous_zone: str | None
    crossed: bool
    notes: list[str]
    record: dict


def _as_of_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _fmt_price(price: float, currency: str) -> str:
    if price >= 100:
        s = f"{price:.1f}"
    else:
        s = f"{price:.2f}"
    return f"{s} {currency}"


_HA_SUFFIXES = (".HK", ".SH", ".SZ", ".SS")


def _short_name(meta: FrameworkMeta) -> str:
    """Title segment before ' / ' (中文名优先)."""
    name = (meta.name or "").strip()
    if " / " in name:
        name = name.split(" / ", 1)[0].strip()
    return name or meta.ticker


def row_symbol(meta: FrameworkMeta) -> str:
    """H/A: 简称（code）内联；美股等：裸 code。"""
    if meta.ticker.endswith(_HA_SUFFIXES):
        return f"{_short_name(meta)}（{meta.ticker}）"
    return meta.ticker


def _display_width(s: str) -> int:
    return sum(2 if ord(ch) > 0x7F else 1 for ch in s)


def _pad_display(s: str, width: int) -> str:
    return s + " " * max(1, width - _display_width(s))


def format_pool_row(r: Row, *, sym_width: int = 22) -> str:
    """🟢  腾讯控股（0700.HK）  绩优              深度折价"""
    emoji = zone_emoji(r.zone)
    sym_col = _pad_display(row_symbol(r.meta), sym_width)
    lynch_col = _pad_display(r.meta.lynch_label_cn, 16)
    return f"{emoji}  {sym_col}{lynch_col} {zone_label(r.zone)}"


def build_daily_post(rows: list[Row], *, as_of_local: str) -> tuple[str, list[str]]:
    """Rich-text daily digest lines (Feishu post body)."""
    title = f"lynch 价带日报 · {as_of_local}"
    # Column width follows this run's widest name; a fixed 22 shifted the
    # Lynch column on long names like 阿里巴巴集团（9988.HK）.
    width = max((_display_width(row_symbol(r.meta)) for r in rows), default=20) + 2
    lines = [format_pool_row(r, sym_width=width) for r in rows]
    first_run = sum(1 for r in rows if "first_run" in r.notes)
    if first_run:
        lines.append("")
        lines.append(
            f"⚠️ first_run {first_run}/{len(rows)}——本机无历史 bands，跨带告警本次不会触发"
        )
    return title, lines


def build_cross_post(crosses: list[Row], *, as_of_local: str) -> tuple[str, list[str]]:
    """Zone-change alert lines; caller @所有人 via send_post."""
    title = f"lynch 跨带 · {as_of_local}"
    lines: list[str] = []
    for r in crosses:
        prev = r.previous_zone or "?"
        lines.append(format_pool_row(r))
        lines.append(
            f"  {prev} → {r.zone}（{zone_label(prev)} → {zone_label(r.zone)}）· "
            f"{_fmt_price(r.price, r.meta.currency)}"
        )
        L, M, H = r.meta.L, r.meta.M, r.meta.H
        lines.append(f"  L/M/H = {L:g}/{M:g}/{H:g}")
    return title, lines


def _send_post(title: str, lines: list[str], *, at_all: bool, dry_run: bool) -> dict:
    result = send_post(title, lines, at_all=at_all, dry_run=dry_run)
    if result.get("skipped_reason") == "missing_webhook" and not dry_run:
        log.error("Feishu webhook missing (set FEISHU_ALERT_WEBHOOK or reuse invest policies)")
    elif not result.get("ok"):
        log.error("Feishu send failed: %s", result)
    else:
        log.info(
            "feishu ok dry_run=%s at_all=%s title=%s",
            result.get("dry_run"),
            result.get("at_all"),
            title,
        )
    return result



def run(*, dry_run: bool, notify: bool, bands_dir: Path | None) -> int:
    # A parse failure or a quote failure used to kill the cron before it could
    # say so; both now degrade to an alert / per-ticker error instead.
    try:
        metas = load_all_frameworks()
    except Exception as e:  # noqa: BLE001
        log.error("framework parse failed: %s", e)
        _send_post(
            f"lynch 价带日报 · 解析失败 · {datetime.now(TZ).strftime('%Y-%m-%d %H:%M %Z')}",
            ["【框架解析失败，本次未出日报】", str(e)],
            at_all=False,
            dry_run=dry_run or not notify,
        )
        return 2

    symbols = [m.ticker for m in metas]
    rows: list[Row] = []
    errors: list[str] = []
    try:
        quotes = fetch_quotes(symbols, allow_missing=True)
    except Exception as e:  # noqa: BLE001
        log.error("quote fetch failed: %s", e)
        quotes = {}
        errors.append(f"报价整体失败: {e}")

    for meta in metas:
        notes: list[str] = []
        try:
            if meta.ticker not in quotes:
                raise KeyError("longbridge 未返回报价（停牌/代码变更/接口异常）")
            q = quotes[meta.ticker]
            price = q.last
            zone = zone_of(price, meta.L, meta.M, meta.H)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{meta.ticker}: {e}")
            continue

        prev = read_previous(meta.ticker, bands_dir)
        previous_zone = None
        if prev and isinstance(prev.get("zone"), str):
            previous_zone = prev["zone"]
        crossed = previous_zone is not None and previous_zone != zone
        if previous_zone is None:
            notes.append("first_run")

        record = build_record(
            ticker=meta.ticker,
            as_of=_as_of_iso(),
            price=price,
            currency=meta.currency,
            L=meta.L,
            M=meta.M,
            H=meta.H,
            zone=zone,
            previous_zone=previous_zone,
            notes=notes,
            inputs_used={
                "price_source": "longbridge",
                "price_field": "last",
                "quote_status": q.status,
                "anchors_source": str(meta.path.relative_to(REPO_ROOT)),
            },
        )
        if not dry_run:
            write_bands(record, bands_dir)
        rows.append(
            Row(
                meta=meta,
                price=price,
                zone=zone,
                previous_zone=previous_zone,
                crossed=crossed,
                notes=notes,
                record=record,
            )
        )

    if not rows and errors:
        log.error("no rows computed: %s", "; ".join(errors))
        return 2

    crosses = [r for r in rows if r.crossed]
    as_of_local = datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z")
    daily_title, daily_lines = build_daily_post(rows, as_of_local=as_of_local)
    if errors:
        daily_lines.append("")
        daily_lines.append("【失败】")
        daily_lines.extend(f"• {e}" for e in errors)

    log.info("computed %d tickers, crosses=%d", len(rows), len(crosses))
    print(daily_title)
    print("\n".join(daily_lines))
    if crosses:
        cross_title, cross_lines = build_cross_post(crosses, as_of_local=as_of_local)
        print()
        print(cross_title)
        print("\n".join(cross_lines))
    else:
        cross_title = ""
        cross_lines: list[str] = []

    exit_code = 1 if errors else 0

    if notify or dry_run:
        # Always: daily post (no @all). Separately: cross post with @所有人.
        r1 = _send_post(
            daily_title, daily_lines, at_all=False, dry_run=dry_run or not notify
        )
        if notify and not dry_run and not r1.get("ok"):
            return 2
        if crosses:
            r2 = _send_post(
                cross_title, cross_lines, at_all=True, dry_run=dry_run or not notify
            )
            if notify and not dry_run and not r2.get("ok"):
                return 2

    return exit_code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="do not write bands JSON; Feishu payload logged only",
    )
    p.add_argument(
        "--no-notify",
        action="store_true",
        help="skip Feishu (still write bands unless --dry-run)",
    )
    p.add_argument(
        "--bands-dir",
        type=Path,
        default=None,
        help="override output/bands directory",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(
        dry_run=args.dry_run,
        notify=not args.no_notify,
        bands_dir=args.bands_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
