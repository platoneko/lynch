"""Read shared scenario axes from frameworks/_AXIS.*.md.

A shared axis is the one place a driver's scenario ladder is written down; every
mounted ticker's eval reads it from here rather than restating the numbers, so
editing one side alone turns the other side's eval red (AGENTS.md「锚点语义」).
"""
from __future__ import annotations

import re
from pathlib import Path

FRAMEWORKS_DIR = Path(__file__).resolve().parents[2] / "frameworks"
AXIS_COWOS = FRAMEWORKS_DIR / "_AXIS.cowos-capacity.md"
AXIS_CAPEX = FRAMEWORKS_DIR / "_AXIS.ai-capex.md"

# §2 column order: 状态 | 分配规则 | NVDA | AMD | AVGO | 其余
COWOS_PLAYERS = ("NVDA", "AMD", "AVGO", "OTHER")
COWOS_SUPPLY = 2.400e6  # wafers/year — §1, TSMC 200k wpm

_STATE_ROW = re.compile(r"^\|\s*\*\*([αβγ])\*\*")
_WAN = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*万")


def load_cowos_quota(path: Path | None = None) -> dict[str, dict[str, float]]:
    """{state: {player: wafers}} from the α/β/γ allocation table.

    Raises if a row is malformed or an allocation does not exhaust supply — a
    state whose shares do not sum to the capacity is not an allocation at all.
    """
    md = (path or AXIS_COWOS).read_text(encoding="utf-8")

    states: dict[str, dict[str, float]] = {}
    for line in md.splitlines():
        m = _STATE_ROW.match(line)
        if not m:
            continue
        wafers = [float(x) * 1e4 for x in _WAN.findall(line)]
        if len(wafers) != len(COWOS_PLAYERS):
            raise ValueError(
                f"axis state {m.group(1)}: expected {len(COWOS_PLAYERS)} quotas, "
                f"got {len(wafers)}"
            )
        states[m.group(1)] = dict(zip(COWOS_PLAYERS, wafers))

    missing = {"α", "β", "γ"} - states.keys()
    if missing:
        raise ValueError(f"axis states missing: {sorted(missing)}")

    for state, quotas in states.items():
        total = sum(quotas.values())
        if abs(total - COWOS_SUPPLY) > 1e4:  # rounded to 0.1万 in the table
            raise ValueError(
                f"axis state {state}: quotas sum to {total:.0f}, "
                f"supply is {COWOS_SUPPLY:.0f}"
            )
    return states


def check_cowos_supply(wpm_2027: float) -> list[str]:
    """Verify a supplier-side mount states the same 2027 capacity as the axis.

    TSMC sits on this axis as the supplier, not as a claimant: the α/β/γ states
    do not move its anchors (axis §3), but the capacity they divide up is its
    own B-leg input, so the two must not drift apart.
    """
    expected_wpm = COWOS_SUPPLY / 12.0
    if abs(wpm_2027 - expected_wpm) > 1e3:
        return [
            f"2027 capacity: stated {wpm_2027:.0f} wpm vs axis "
            f"{expected_wpm:.0f} wpm ({COWOS_SUPPLY:.0f} wafers/yr)"
        ]
    return []


def check_cowos_mount(
    ticker: str, quota_l: float, quota_m: float, quota_h: float
) -> list[str]:
    """Verify a ticker's stated L/M/H quotas against its axis state mapping.

    The mapping is inverted between competitors (§3): NVDA reads α/β/γ as L/M/H,
    AVGO reads it as H/M/L. Getting this wrong is exactly the failure the
    zero-sum axis exists to catch — both sides assuming they lose share.
    """
    mapping = {"NVDA": ("α", "β", "γ"), "AVGO": ("γ", "β", "α")}
    if ticker not in mapping:
        raise ValueError(f"{ticker} is not a quota-driven mount; see axis §3")

    states = load_cowos_quota()
    errors: list[str] = []
    for label, state, stated in zip(
        ("L", "M", "H"), mapping[ticker], (quota_l, quota_m, quota_h)
    ):
        expected = states[state][ticker]
        if abs(stated - expected) > 1e4:
            errors.append(
                f"{label} quota: stated {stated:.0f} vs axis state {state} "
                f"{expected:.0f}"
            )
    return errors


# §1 rung rows: | **L** | **$1.06 万亿** | +32% | ... |
_CAPEX_ROW = re.compile(r"^\|\s*\*\*([LMH])\*\*\s*\|\s*\*\*\$([0-9.]+)\s*万亿\*\*")
CAPEX_BASE_2026 = 8.0e11  # §1 base: ~$800B, NVDA CFO wording


def load_capex_rungs(path: Path | None = None) -> dict[str, float]:
    """{tier: 2027 capex in USD} from _AXIS.ai-capex.md §1."""
    md = (path or AXIS_CAPEX).read_text(encoding="utf-8")
    rungs = {
        m.group(1): float(m.group(2)) * 1e12
        for m in (_CAPEX_ROW.match(line) for line in md.splitlines())
        if m
    }
    missing = {"L", "M", "H"} - rungs.keys()
    if missing:
        raise ValueError(f"capex axis rungs missing: {sorted(missing)}")
    if not (rungs["L"] < rungs["M"] < rungs["H"]):
        raise ValueError(f"capex rungs not ordered: {rungs}")
    return rungs


def check_capex_mount(stated: tuple[float, float, float]) -> list[str]:
    """Demand-side mount: the ticker's stated rungs must be the axis's own."""
    rungs = load_capex_rungs()
    return [
        f"{tier} capex rung: stated {got:.4g} vs axis {rungs[tier]:.4g}"
        for tier, got in zip(("L", "M", "H"), stated)
        if abs(got - rungs[tier]) > 1e9
    ]


def check_capex_constituent(guide_low: float, guide_high: float) -> list[str]:
    """Constituent-side mount (axis §2b): the ticker's own capex is *inside* the
    axis base, so the only coherence test is that its guided spend is a sane
    share of the five-buyer pool — a share above 1 means the base is stale.
    """
    errors: list[str] = []
    if guide_low > guide_high:
        errors.append(f"capex guide inverted: {guide_low:.4g} > {guide_high:.4g}")
    share = guide_high / CAPEX_BASE_2026
    if not (0.0 < share < 1.0):
        errors.append(
            f"capex guide {guide_high:.4g} is {share:.1%} of the axis 2026 base "
            f"{CAPEX_BASE_2026:.4g} — base or guide is stale (axis §1/§2b)"
        )
    return errors


def capex_constituent_share(guide_low: float, guide_high: float) -> tuple[float, float]:
    """Printable share-of-pool range for a constituent mount."""
    return guide_low / CAPEX_BASE_2026, guide_high / CAPEX_BASE_2026
