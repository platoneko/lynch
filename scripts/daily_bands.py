#!/usr/bin/env python3
"""Daily: quote → zone from framework L/M/H → write bands → Feishu notify.

Always sends a daily digest. If any ticker's zone changed vs previous artifact
(and previous_zone was not null), the message @所有人.

Does NOT write frameworks/*.md.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.axes import load_rates  # noqa: E402
from eval.lib.bands_io import build_record, read_previous, read_rf_readings, write_bands  # noqa: E402
from eval.lib.feishu import send_post  # noqa: E402
from eval.lib.frameworks import FrameworkMeta, load_all_frameworks  # noqa: E402
from eval.lib.price import fetch_quotes  # noqa: E402
# candidate_zone / credited_option_zone are re-exported: the digest tests call them from here.
from eval.lib.zones import (  # noqa: E402, F401
    candidate_zone,
    credited_option_zone,
    price_zones,
    zone_bound_edges,
    zone_emoji,
    zone_label,
    zone_of,
)


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
    option_zone: str | None = None
    previous_option_zone: str | None = None
    option_crossed: bool = False
    candidate_zone: str | None = None
    previous_candidate_zone: str | None = None
    candidate_crossed: bool = False
    # r_f readings from output/rates/ (eval-written); readings only, never a cross.
    rf: dict | None = None

    @property
    def any_crossed(self) -> bool:
        return self.crossed or self.option_crossed or self.candidate_crossed


def _as_of_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# A-share exchange-traded funds (SH 5xxxxx, SZ 15xxxx–18xxxx) tick at 0.001, stocks at 0.01.
_A_SHARE_FUND = re.compile(r"^(5\d{5}\.SH|1[5-8]\d{4}\.SZ)$")


def price_decimals(ticker: str) -> int:
    """Decimals below 100 for `ticker`'s prices: its quote tick, so a 0.001 move shows."""
    return 3 if _A_SHARE_FUND.match(ticker) else 2


def _fmt_num(price: float, dp: int = 2) -> str:
    """Same precision as Feishu price display (≥100 → 1dp, else `dp`)."""
    if price >= 100:
        return f"{price:.1f}"
    return f"{price:.{dp}f}"


def _fmt_price(price: float, currency: str, dp: int = 2) -> str:
    return f"{_fmt_num(price, dp)} {currency}"


def _fmt_anchor(x: float, dp: int = 2) -> str:
    """`dp` decimals, trailing zeros stripped (`27.2`, `246.44`, `113`)."""
    s = f"{x:.{dp}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def format_lmh(L: float, M: float, H: float, dp: int = 2) -> str:
    return f"L/M/H = {_fmt_anchor(L, dp)}/{_fmt_anchor(M, dp)}/{_fmt_anchor(H, dp)}"


def format_zone_bounds(zone: str, L: float, M: float, H: float, dp: int = 2) -> str:
    """Current-zone lo/hi in AGENTS bracket notation (`[A, B)` / `P < L`)."""
    shape, lo, hi = zone_bound_edges(zone, L, M, H)
    if shape == "open_below":
        assert hi is not None
        return f"P < {_fmt_anchor(hi, dp)}"
    if shape == "open_above":
        assert lo is not None
        return f"P > {_fmt_anchor(lo, dp)}"
    assert lo is not None and hi is not None
    lo_s, hi_s = _fmt_anchor(lo, dp), _fmt_anchor(hi, dp)
    if shape == "half_open":
        return f"[{lo_s}, {hi_s})"
    if shape == "closed":
        return f"[{lo_s}, {hi_s}]"
    raise ValueError(f"unknown shape: {shape}")


def zone_changed(previous: str | None, current: str | None) -> bool:
    """True only when both sides are known and differ. None = first observation, no alert."""
    return previous is not None and current is not None and previous != current


def _short_name(meta: FrameworkMeta) -> str:
    """Title segment before ' / ' (中文名优先)."""
    name = (meta.name or "").strip()
    if " / " in name:
        name = name.split(" / ", 1)[0].strip()
    return name or meta.ticker


def format_pool_lines(r: Row) -> list[str]:
    """Per-ticker Feishu block.

    🟡 603993.SH 洛阳钼业 19.01 CNY
    「中性」 [16.57, 21.44) · L/M/H = 13.51/19.63/23.24

    Tickers with V_opt > 0 get a third line on the credited band:
    「乐观」 +V(66.6) [71.17, 113] · L/M/H = 27.2/71.17/113
    Tickers carrying a candidate band get one more, last:
    「叙事溢价」 候选(FY28·Piper单家) P > 16.29 · L/M/H = 7.23/15.54/16.29
    """
    title = r.meta.ticker
    if not r.meta.ticker.endswith(".US"):
        title = f"{r.meta.ticker} {_short_name(r.meta)}"
    emoji = zone_emoji(r.zone)
    dp = price_decimals(r.meta.ticker)
    header = f"{emoji} {title} {_fmt_price(r.price, r.meta.currency, dp)}"
    bounds = format_zone_bounds(r.zone, r.meta.L, r.meta.M, r.meta.H, dp)
    lines = [
        header,
        f"「{zone_label(r.zone)}」 {bounds} · {format_lmh(r.meta.L, r.meta.M, r.meta.H, dp)}",
    ]
    band = r.meta.option_band
    v_opt = r.meta.v_opt
    if (
        band is not None
        and v_opt is not None
        and v_opt > 0
        and r.option_zone
    ):
        L2, M2, H2 = band
        ob = format_zone_bounds(r.option_zone, L2, M2, H2, dp)
        lines.append(
            f"「{zone_label(r.option_zone)}」 +V({_fmt_anchor(v_opt, dp)}) {ob} · {format_lmh(L2, M2, H2, dp)}"
        )
    if r.meta.candidate is not None and r.candidate_zone:
        L3, M3, H3 = r.meta.candidate
        cb = format_zone_bounds(r.candidate_zone, L3, M3, H3, dp)
        lines.append(
            f"「{zone_label(r.candidate_zone)}」 候选({r.meta.candidate_label}) "
            f"{cb} · {format_lmh(L3, M3, H3, dp)}"
        )
    lines.extend(format_rf_lines(r))
    return lines


def format_rf_lines(r: Row) -> list[str]:
    """Two r_f reading lines (valuation-rules「利率轴」): consensus Δ, then reverse pressure.

    「叙事溢价」 利率·共识(+75bp) P > 312.63 · L/M/H = 215.7/264.9/312.63
    「中性」 利率·压力±50bp [266.24, 355.63) · L/M/H = 225.87/306.6/404.67
    """
    if r.rf is None:
        return []
    if r.rf["stale"]:
        return [f"利率读数过期（利率轴或锚点已变，重跑 frameworks/eval/{r.meta.ticker}.py）"]
    dp = price_decimals(r.meta.ticker)
    out = []
    for label, band in (
        (f"利率·共识({r.rf['delta_cons'] * 1e4:+.0f}bp)", r.rf["consensus"]),
        (f"利率·压力±{r.rf['rf_cap'] * 1e4:.0f}bp", r.rf["pressure"]),
    ):
        z = zone_of(r.price, *band)
        out.append(f"「{zone_label(z)}」 {label} {format_zone_bounds(z, *band, dp)} · {format_lmh(*band, dp)}")
    return out


def _join_ticker_blocks(blocks: list[list[str]]) -> list[str]:
    lines: list[str] = []
    for i, block in enumerate(blocks):
        if i:
            lines.append("")
        lines.extend(block)
    return lines


def build_daily_post(rows: list[Row], *, as_of_local: str) -> tuple[str, list[str]]:
    """Rich-text daily digest lines (Feishu post body)."""
    title = f"lynch 价带日报 · {as_of_local}"
    lines = _join_ticker_blocks([format_pool_lines(r) for r in rows])
    first_run = sum(1 for r in rows if "first_run" in r.notes)
    if first_run:
        lines.append("")
        lines.append(
            f"⚠️ first_run {first_run}/{len(rows)}——本机无历史 bands，跨带告警本次不会触发"
        )
    return title, lines


def build_cross_post(crosses: list[Row], *, as_of_local: str) -> tuple[str, list[str]]:
    """Zone-change alert lines; caller @所有人 via send_post.

    Base-deck and +V-deck crosses are independent: either one (or both) emits
    a transition line under the same snapshot block.
    """
    title = f"lynch 跨带 · {as_of_local}"
    blocks: list[list[str]] = []
    for r in crosses:
        block = format_pool_lines(r)
        if r.crossed:
            prev = r.previous_zone or "?"
            block.append(f"「{zone_label(prev)}」 → 「{zone_label(r.zone)}」")
        if r.option_crossed:
            prev_o = r.previous_option_zone or "?"
            cur_o = r.option_zone or "?"
            block.append(f"+V 「{zone_label(prev_o)}」 → 「{zone_label(cur_o)}」")
        if r.candidate_crossed:
            prev_c = r.previous_candidate_zone or "?"
            cur_c = r.candidate_zone or "?"
            block.append(f"候选 「{zone_label(prev_c)}」 → 「{zone_label(cur_c)}」")
        blocks.append(block)
    return title, _join_ticker_blocks(blocks)


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

    # _AXIS.rates segment by listing currency; an unbuilt segment (CNY / TWD)
    # simply prints no r_f lines.
    rate_segs: dict[str, dict[str, float] | None] = {}
    for ccy in {m.currency for m in metas}:
        try:
            rate_segs[ccy] = load_rates(ccy)
        except ValueError:
            rate_segs[ccy] = None

    for meta in metas:
        notes: list[str] = []
        try:
            if meta.ticker not in quotes:
                raise KeyError("longbridge 未返回报价（停牌/代码变更/接口异常）")
            q = quotes[meta.ticker]
            price = q.last
            zone, option_zone, cand_zone = price_zones(price, meta)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{meta.ticker}: {e}")
            continue

        prev = read_previous(meta.ticker, bands_dir)
        previous_zone = None
        previous_option_zone = None
        if prev and isinstance(prev.get("zone"), str):
            previous_zone = prev["zone"]
        if prev and isinstance(prev.get("option_zone"), str):
            previous_option_zone = prev["option_zone"]
        previous_candidate_zone = None
        if prev and isinstance(prev.get("candidate_zone"), str):
            previous_candidate_zone = prev["candidate_zone"]
        crossed = zone_changed(previous_zone, zone)
        option_crossed = zone_changed(previous_option_zone, option_zone)
        candidate_crossed = zone_changed(previous_candidate_zone, cand_zone)
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
            option_zone=option_zone,
            previous_option_zone=previous_option_zone,
            candidate_zone=cand_zone,
            previous_candidate_zone=previous_candidate_zone,
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
                option_zone=option_zone,
                previous_option_zone=previous_option_zone,
                option_crossed=option_crossed,
                candidate_zone=cand_zone,
                previous_candidate_zone=previous_candidate_zone,
                candidate_crossed=candidate_crossed,
                rf=(
                    read_rf_readings(meta.ticker, seg, (meta.L, meta.M, meta.H))
                    if (seg := rate_segs.get(meta.currency))
                    else None
                ),
            )
        )

    if not rows and errors:
        log.error("no rows computed: %s", "; ".join(errors))
        return 2

    crosses = [r for r in rows if r.any_crossed]
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
