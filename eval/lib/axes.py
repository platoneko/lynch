"""Read shared scenario axes from frameworks/axes/<name>.md.

Logical id in docs stays `_AXIS.<name>` (stable across path moves). Files live
under frameworks/axes/; resolve with axis_path(). Every mounted ticker's eval
reads ladder values from here rather than restating them, so editing one side
alone turns the other side's eval red (valuation-rules.md「共享情景轴」).
"""
from __future__ import annotations

import re
from pathlib import Path

FRAMEWORKS_DIR = Path(__file__).resolve().parents[2] / "frameworks"
AXES_DIR = FRAMEWORKS_DIR / "axes"


def axis_path(axis_id: str) -> Path:
    """Resolve `_AXIS.foo`, `_AXIS.foo.md`, or bare `foo` → frameworks/axes/foo.md."""
    name = axis_id.strip().removeprefix("_AXIS.").removesuffix(".md")
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"invalid axis id: {axis_id!r}")
    return AXES_DIR / f"{name}.md"


def list_axis_paths(axes_dir: Path | None = None) -> list[Path]:
    d = axes_dir or AXES_DIR
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.md") if not p.name.startswith("_"))


AXIS_COWOS = axis_path("cowos-capacity")
AXIS_CAPEX = axis_path("ai-capex")
AXIS_AU_CU = axis_path("fed-path-gold-copper")
AXIS_AL = axis_path("fed-path-aluminium")
AXIS_LI2CO3 = axis_path("lithium-carbonate")
AXIS_PHOS = axis_path("phosphate-chain")
AXIS_AFRICA = axis_path("africa-corridor")
AXIS_OPTICAL = axis_path("optical-comms")
AXIS_COAL = axis_path("coal-price")
AXIS_BRENT = axis_path("brent-oil")
AXIS_SOUTHBOUND = axis_path("southbound-repricing")
AXIS_CRYPTO = axis_path("crypto-cycle")
AXIS_RATES = axis_path("rates")

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


# --- _AXIS.frontier-lab-funding ----------------------------------------------
# §3b holds the ONE arrival haircut every lab leg shares: the five sellers are
# priced on the same question (will OpenAI / Anthropic pay from their own
# revenue), so a per-ticker k would let the same risk be priced five ways.
# §3b row: | `k_lab` | **0.30** | source |
AXIS_LAB = axis_path("frontier-lab-funding")
_K_LAB_ROW = re.compile(r"^\|\s*`k_lab`\s*\|\s*\*\*([0-9.]+)\*\*")


def load_k_lab(path: Path | None = None) -> float:
    for line in (path or AXIS_LAB).read_text(encoding="utf-8").splitlines():
        m = _K_LAB_ROW.match(line)
        if m:
            return float(m.group(1))
    raise ValueError("k_lab row missing from _AXIS.frontier-lab-funding §3b")


def check_lab_mount(k_stated: float) -> list[str]:
    """A lab leg's k MUST be the axis value, cell for cell."""
    k_axis = load_k_lab()
    if abs(k_stated - k_axis) > 1e-9:
        return [f"k_lab {k_stated} != _AXIS.frontier-lab-funding §3b {k_axis} — "
                f"change the axis (all five mounts move), never one ticker"]
    return []


# --- _AXIS.fed-path-gold-copper ----------------------------------------------
# §1 rung rows: | **L** | 加息 | **3,942** | **9,431.05** | source_au | source_cu | as-of |
# Column 2 = gold $/oz, column 3 = copper $/t (0-based).
_AU_CU_ROW = re.compile(r"^\|\s*\*\*([LMH])\*\*\s*\|")


def load_au_cu_rungs(path: Path | None = None) -> dict[str, dict[str, float]]:
    """{metal: {tier: USD}} for metal in ('au','cu') from the axis §1 table."""
    md = (path or AXIS_AU_CU).read_text(encoding="utf-8")

    au: dict[str, float] = {}
    cu: dict[str, float] = {}
    for line in md.splitlines():
        m = _AU_CU_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            raise ValueError(f"gold-copper axis rung {m.group(1)}: expected >=4 columns")
        tier = m.group(1)
        # Skip aluminium-style rows that somehow match — require numeric gold col.
        try:
            au[tier] = float(cells[2].strip("* ").replace(",", ""))
            cu[tier] = float(cells[3].strip("* ").replace(",", ""))
        except ValueError:
            continue

    for metal, rungs in (("au", au), ("cu", cu)):
        missing = {"L", "M", "H"} - rungs.keys()
        if missing:
            raise ValueError(f"gold-copper axis {metal} rungs missing: {sorted(missing)}")
        if not (rungs["L"] < rungs["M"] < rungs["H"]):
            raise ValueError(f"gold-copper axis {metal} rungs not ordered: {rungs}")
    return {"au": au, "cu": cu}


def copper_upside_ratio(path: Path | None = None) -> float:
    """Copper H/M from the gold-copper axis — aluminium upside must stay below."""
    cu = load_au_cu_rungs(path)["cu"]
    return cu["H"] / cu["M"]


def check_au_cu_mount(
    stated_au: tuple[float, float, float],
    stated_cu: tuple[float, float, float],
    path: Path | None = None,
) -> list[str]:
    """Verify a mounted ticker restates the gold and copper USD rungs."""
    rungs = load_au_cu_rungs(path)
    errors: list[str] = []
    for tier, got in zip(("L", "M", "H"), stated_au):
        want = rungs["au"][tier]
        if abs(got - want) > 1e-6:
            errors.append(f"{tier} gold rung: stated {got:g} vs axis {want:g}")
    for tier, got in zip(("L", "M", "H"), stated_cu):
        want = rungs["cu"][tier]
        if abs(got - want) > 1e-6:
            errors.append(f"{tier} copper rung: stated {got:g} vs axis {want:g}")
    return errors


# --- _AXIS.lithium-carbonate -------------------------------------------------
# §1 rung rows: | **L** | 供应过剩回归 | **120,000** | sources | as-of |
_LI2CO3_ROW = re.compile(r"^\|\s*\*\*([LMH])\*\*\s*\|")


def load_li2co3_rungs(path: Path | None = None) -> dict[str, float]:
    """{tier: battery-grade Li2CO3 CNY per tonne} from the axis §1 table."""
    md = (path or AXIS_LI2CO3).read_text(encoding="utf-8")
    rungs: dict[str, float] = {}
    for line in md.splitlines():
        m = _LI2CO3_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        try:
            rungs[m.group(1)] = float(cells[2].strip("* ").replace(",", ""))
        except ValueError:
            continue
    missing = {"L", "M", "H"} - rungs.keys()
    if missing:
        raise ValueError(f"lithium-carbonate axis rungs missing: {sorted(missing)}")
    if not (rungs["L"] < rungs["M"] < rungs["H"]):
        raise ValueError(f"lithium-carbonate axis rungs not ordered: {rungs}")
    return rungs


def check_li2co3_mount(
    stated: tuple[float, float, float],
    path: Path | None = None,
) -> list[str]:
    """Verify a mounted ticker restates the Li2CO3 CNY rungs."""
    rungs = load_li2co3_rungs(path)
    errors: list[str] = []
    for tier, got in zip(("L", "M", "H"), stated):
        want = rungs[tier]
        if abs(got - want) > 1e-6:
            errors.append(f"{tier} li2co3 rung: stated {got:g} vs axis {want:g}")
    return errors


# --- _AXIS.fed-path-aluminium ------------------------------------------------
# §1 rung rows: | **L** | 供应过剩兑现 × … | **4,250** | 19,512 | 2,713 | source | as-of |
# Column 2 (0-based) is the SHFE smelting spread every mounted ticker reads.
# Column 3 is the same rung translated to SHFE spot with the cost basket frozen
# (§1); it is kept only for the copper-upside inequality, which is a statement
# about price, not margin.  Parse by column: the bolding differs across rows.
_AL_ROW = re.compile(r"^\|\s*\*\*([LMH])\*\*\s*\|")
AL_COST_BASKET = 15262.0  # CNY/t, §1: 2026H1 spot 24,141 − GS 2026H1 spread 8,879


def load_al_rungs(path: Path | None = None) -> dict[str, dict[str, float]]:
    """{'spread': {tier: CNY/t}, 'price': {tier: CNY/t}} from the axis §1 table.

    Raises if the two columns do not differ by the frozen cost basket on every
    rung — the price column is a translation of the spread column, and a rung
    where they drift apart has been edited on one side only.
    """
    md = (path or AXIS_AL).read_text(encoding="utf-8")

    spread: dict[str, float] = {}
    price: dict[str, float] = {}
    for line in md.splitlines():
        m = _AL_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            raise ValueError(f"axis rung {m.group(1)}: expected >=4 columns")
        spread[m.group(1)] = float(cells[2].strip("* ").replace(",", ""))
        price[m.group(1)] = float(cells[3].strip("* ").replace(",", ""))

    missing = {"L", "M", "H"} - spread.keys()
    if missing:
        raise ValueError(f"aluminium axis rungs missing: {sorted(missing)}")
    if not (spread["L"] < spread["M"] < spread["H"]):
        raise ValueError(f"aluminium spread rungs not ordered: {spread}")
    for tier in ("L", "M", "H"):
        if abs(price[tier] - spread[tier] - AL_COST_BASKET) > 1.0:
            raise ValueError(
                f"aluminium rung {tier}: price {price[tier]:g} − spread "
                f"{spread[tier]:g} ≠ cost basket {AL_COST_BASKET:g} (§1)"
            )
    return {"spread": spread, "price": price}


def check_al_mount(stated: tuple[float, float, float]) -> list[str]:
    """Verify a mounted ticker restates the axis spread rungs, and that the
    axis keeps aluminium's modelled price upside below copper's.

    The second check is the whole reason this axis is separate from the gold /
    copper ladder despite sharing the same Fed-path regimes: aluminium's supply
    is restartable (EGA has already relit 315 of 1,262 pots) and its deficit is
    0.68% of demand, so a copper-sized H would be an unearned upside.
    """
    rungs = load_al_rungs()
    spread, price = rungs["spread"], rungs["price"]
    errors = [
        f"{tier} smelting spread: stated {got:g} vs axis {spread[tier]:g}"
        for tier, got in zip(("L", "M", "H"), stated)
        if abs(got - spread[tier]) > 0.5
    ]

    cu_ratio = copper_upside_ratio()
    upside = price["H"] / price["M"]
    if upside >= cu_ratio:
        errors.append(
            f"aluminium price H/M {upside:.4f} >= copper's {cu_ratio:.4f} — "
            "axis §1 says aluminium's shortage is the softer one"
        )
    return errors


# --- shared perpetual g2 (valuation-rules.md「g2 来源优先序」第 2 层 · 共享轴) -----------
# A commodity axis names the driver's long-run growth ONCE, as a labelled row
#   | **永续 g2（2026-09-14）** | **0%** | 来源 | as-of |
# and, for its cyclical mounts, the tier shape (skill A2e-5: flat or reverse):
#   | **g_L** | **0%** | 低位 regime 持续（与平台同）：… | as-of |
# Every mounted ticker's Gordon tiers read these values from here, so a g edited
# on one side turns the other side's eval red — the same rule as the price rungs.
_AXIS_G2_ROW = re.compile(
    r"永续 g2（(\d{4}-\d{2}-\d{2})）\*{0,2}\s*[：:|]\s*\*{0,2}([+-]?[0-9]+(?:\.[0-9]+)?)\s*%"
)
_AXIS_G_TIER_ROW = re.compile(
    r"^\|\s*\*\*g_([LMH])\*\*\s*\|\s*\*{0,2}([+-]?[0-9]+(?:\.[0-9]+)?)\s*%"
)


def load_axis_g2(path: Path) -> float:
    """The axis's labelled `永续 g2（date）` value — the M-tier perpetual g."""
    labelled = _AXIS_G2_ROW.search(path.read_text(encoding="utf-8"))
    if labelled is None:
        raise ValueError(f"{path.name}: no labelled 永续 g2（YYYY-MM-DD） row")
    return float(labelled.group(2)) / 100.0


def check_axis_g2_mount(path: Path, g2_m: float) -> list[str]:
    """Verify a mount's M-tier g equals the axis's labelled g2 (mounts whose
    L/H tiers are ticker-specific — e.g. a realised-recovery two-stage L —
    check only this row)."""
    want = load_axis_g2(path)
    if abs(g2_m - want) > 1e-9:
        return [f"M perpetual g: stated {g2_m:+.2%} vs axis 永续 g2 {want:+.2%}"]
    return []


def load_axis_g_tiers(path: Path) -> dict[str, float]:
    """{tier: perpetual g} from an axis file's 永续 g2 block.

    The labelled `永续 g2（date）` row is the M tier — the driver's long-run
    growth with a named source.  The g_L / g_H rows are the cyclical tier
    shape and must be flat-or-reverse around it: a same-direction shape would
    re-price the cycle a second time on top of the numerator (skill A2e-5).
    """
    md = path.read_text(encoding="utf-8")
    g2 = load_axis_g2(path)

    tiers: dict[str, float] = {}
    for line in md.splitlines():
        m = _AXIS_G_TIER_ROW.match(line)
        if m:
            tiers[m.group(1)] = float(m.group(2)) / 100.0
    missing = {"L", "M", "H"} - tiers.keys()
    if missing:
        raise ValueError(f"{path.name}: g tier rows missing: {sorted(missing)}")
    if abs(tiers["M"] - g2) > 1e-9:
        raise ValueError(
            f"{path.name}: g_M row {tiers['M']:.4f} ≠ labelled 永续 g2 {g2:.4f}"
        )
    if not (tiers["L"] >= tiers["M"] >= tiers["H"]):
        raise ValueError(
            f"{path.name}: g tiers {tiers} are not flat-or-reverse (g_L ≥ g_M ≥ g_H)"
        )
    return tiers


def check_axis_g_mount(path: Path, stated: dict[str, float]) -> list[str]:
    """Verify a mounted ticker restates the axis g rows it reads, exactly.

    `stated` maps tier → the mount's perpetual g for that tier.  A mount whose
    L tier walks its own realised recovery (valuation-rules「谷底档」) reads no
    axis g for L and states only M / H.
    """
    tiers = load_axis_g_tiers(path)
    return [
        f"{tier} perpetual g: stated {got:+.2%} vs axis {tiers[tier]:+.2%}"
        for tier, got in stated.items()
        if abs(got - tiers[tier]) > 1e-9
    ]


# --- _AXIS.phosphate-chain ---------------------------------------------------
# §1 rung rows: | **L** | 管制回归 / 涨价回吐 | **3,330** | 3,536 | source | as-of |
# Column 2 (0-based) is the MAP rung both mounts read; the DAP column is a
# parallel observation only.
_PHOS_ROW = re.compile(r"^\|\s*\*\*([LMH])\*\*\s*\|")

# Axis §2: the two mounts read this ladder through completely different second
# layers.  Yuntianhua's measured elasticity is 0.049 — a mount claiming to be
# driven by this ladder while measuring near-zero response would be misreading
# the axis, so mounts declare their own beta and this ceiling catches drift.
PHOS_INERT_BETA_CEILING = 0.20


def load_phosphate_rungs(path: Path | None = None) -> dict[str, float]:
    """{tier: MAP 55% domestic price, CNY per tonne} from the axis §1 table."""
    md = (path or AXIS_PHOS).read_text(encoding="utf-8")

    rungs: dict[str, float] = {}
    for line in md.splitlines():
        m = _PHOS_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            raise ValueError(f"axis rung {m.group(1)}: expected >=3 columns")
        rungs[m.group(1)] = float(cells[2].strip("* ").replace(",", ""))

    missing = {"L", "M", "H"} - rungs.keys()
    if missing:
        raise ValueError(f"phosphate axis rungs missing: {sorted(missing)}")
    if not (rungs["L"] < rungs["M"] < rungs["H"]):
        raise ValueError(f"phosphate rungs not ordered: {rungs}")
    return rungs


def check_phosphate_mount(
    stated: tuple[float, float, float], peer_md: str | None = None
) -> list[str]:
    """Verify a mount restates the axis rungs, and agrees with its peer mount.

    Both mounts are checked against the axis file *and* against each other:
    the axis is the source of truth, but a peer comparison is what catches the
    case where someone edits both the axis and one mount and forgets the other.
    """
    rungs = load_phosphate_rungs()
    errors = [
        f"{tier} MAP rung: stated {got:g} vs axis {rungs[tier]:g}"
        for tier, got in zip(("L", "M", "H"), stated)
        if abs(got - rungs[tier]) > 0.5
    ]
    if peer_md is not None:
        # Imported lazily: lib.inputs imports nothing from here, but keeping the
        # dependency local documents that this is a cross-file read, not a parse.
        from .inputs import parse_inputs

        peer = parse_inputs(peer_md)
        for tier, field, got in zip(
            ("L", "M", "H"), ("map_l", "map_m", "map_h"), stated
        ):
            if field not in peer:
                errors.append(f"peer mount missing {field}")
            elif abs(peer[field] - got) > 0.5:
                errors.append(
                    f"{tier} MAP rung drift vs peer: ours {got:g} vs peer {peer[field]:g}"
                )
    return errors


# --- _AXIS.africa-corridor ---------------------------------------------------
# §1 rung rows: | **L** | 在建项目投产即止 | **0%** | 依据 | as-of |
# Column 2 (0-based) is the nominal perpetual g every mounted ticker reads.
_AFRICA_ROW = re.compile(r"^\|\s*\*\*([LMH])\*\*\s*\|")
_PCT = re.compile(r"([+-]?[0-9]+(?:\.[0-9]+)?)\s*%")

# The axis caps its own upside on purpose (§1): at r=10% a nominal g of +7%
# would put the perpetual factor at 33x, which is extrapolating a ramp forever.
AFRICA_G_CAP = 0.06


def load_africa_rungs(path: Path | None = None) -> dict[str, float]:
    """{tier: nominal perpetual g} from _AXIS.africa-corridor.md §1."""
    md = (path or AXIS_AFRICA).read_text(encoding="utf-8")

    rungs: dict[str, float] = {}
    for line in md.splitlines():
        m = _AFRICA_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            raise ValueError(f"axis rung {m.group(1)}: expected >=3 columns")
        pct = _PCT.search(cells[2])
        if pct is None:
            raise ValueError(f"axis rung {m.group(1)}: no percentage in column 3")
        rungs[m.group(1)] = float(pct.group(1)) / 100.0

    missing = {"L", "M", "H"} - rungs.keys()
    if missing:
        raise ValueError(f"africa axis rungs missing: {sorted(missing)}")
    if not (rungs["L"] < rungs["M"] < rungs["H"]):
        raise ValueError(f"africa rungs not ordered: {rungs}")
    if rungs["H"] > AFRICA_G_CAP:
        raise ValueError(
            f"africa H rung {rungs['H']:.1%} above the axis's own cap "
            f"{AFRICA_G_CAP:.0%} — that is a ramp extrapolated forever (§1)"
        )
    return rungs


def check_africa_mount(stated: tuple[float, float, float]) -> list[str]:
    """Verify a mounted ticker restates the axis's nominal Africa g ladder.

    The axis locks the NOMINAL perpetual g for tickets whose Africa cash flows
    are perpetual (Huaxin's and Keda's own plants).  Jiayou's concession leg
    uses a *window* g derived from PV parity — see
    `check_africa_jiayou_window` — and MUST NOT call this helper with those
    window rates.
    """
    rungs = load_africa_rungs()
    return [
        f"{tier} Africa nominal g: stated {got:+.2%} vs axis {rungs[tier]:+.2%}"
        for tier, got in zip(("L", "M", "H"), stated)
        if abs(got - rungs[tier]) > 1e-9
    ]


def africa_window_from_perp(g_perp: float, r: float, n: float) -> float:
    """Window growth g_w over n years whose annuity PV equals 1/(r − g_perp).

    Solves F(g_w, r, n) = 1/(r − g_perp).  When the perpetual factor reaches
    the g→r limit n/(1+r), g_w returns r (Jiayou then applies an H cap so the
    singular point is never evaluated inside `annuity_factor`).
    """
    if r - g_perp <= 0:
        raise ValueError(f"perpetual factor undefined: g_perp={g_perp} >= r={r}")
    target = 1.0 / (r - g_perp)
    f_lim = n / (1.0 + r)
    if target >= f_lim - 1e-12:
        return r

    def _f(g: float) -> float:
        return (1.0 - ((1.0 + g) / (1.0 + r)) ** n) / (r - g)

    lo, hi = -0.5, r - 1e-12
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if _f(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def check_africa_jiayou_window(
    stated: tuple[float, float, float],
    *,
    r: float,
    n: float,
    h_cap: float,
) -> list[str]:
    """Jiayou second layer: window g from axis PV parity, H capped below r.

    L/M window rates MUST match `africa_window_from_perp(axis_rung)`.
    H MUST equal min(parity_solve(axis_H), h_cap).  After the usual n-year
    conversion, g_eq then equals the axis rung on L/M and sits just under it
    on H — Africa-leg value aligns with perpetual peers without assuming
    concession renewal.
    """
    rungs = load_africa_rungs()
    expected = {
        "L": africa_window_from_perp(rungs["L"], r, n),
        "M": africa_window_from_perp(rungs["M"], r, n),
        "H": min(africa_window_from_perp(rungs["H"], r, n), h_cap),
    }
    errors = [
        f"{tier} Africa window g: stated {got:+.4%} vs PV-parity {expected[tier]:+.4%}"
        for tier, got in zip(("L", "M", "H"), stated)
        if abs(got - expected[tier]) > 5e-5
    ]
    if h_cap >= r - 1e-12:
        errors.append(f"africa_window_h_cap={h_cap:.2%} must be < r={r:.2%}")
    if abs(stated[2] - h_cap) > 5e-5 and abs(expected["H"] - h_cap) < 5e-5:
        # H is at the cap; stated must equal the cap
        errors.append(
            f"H window g stated {stated[2]:+.4%} but cap binds at {h_cap:+.4%}"
        )
    return errors


# --- _AXIS.optical-comms -----------------------------------------------------
# §1 rung rows: | **L** | **39** | **3.9e5** | ...
# Column 2 (0-based) is OE units (scientific); mounted tickers read that column.
_OPTICAL_ROW = re.compile(r"^\|\s*\*\*([LMH])\*\*\s*\|")
_OE_SCI = re.compile(r"\*\*([0-9]+(?:\.[0-9]+)?e[+-]?[0-9]+)\*\*")

# Cross-check unit (axis §1): GS CPO switch OE BoM ASP — not an axis rung.
OPTICAL_OE_ASP_USD = 450.0


def load_optical_oe_rungs(path: Path | None = None) -> dict[str, float]:
    """{tier: OE units} from _AXIS.optical-comms.md §1 (actual shipments)."""
    md = (path or AXIS_OPTICAL).read_text(encoding="utf-8")
    rungs: dict[str, float] = {}
    for line in md.splitlines():
        m = _OPTICAL_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            raise ValueError(f"optical axis rung {m.group(1)}: expected >=3 columns")
        sci = _OE_SCI.search(cells[2])
        if sci is None:
            raise ValueError(
                f"optical axis rung {m.group(1)}: no scientific OE count in column 3"
            )
        rungs[m.group(1)] = float(sci.group(1))
    missing = {"L", "M", "H"} - rungs.keys()
    if missing:
        raise ValueError(f"optical axis rungs missing: {sorted(missing)}")
    if not (rungs["L"] < rungs["M"] < rungs["H"]):
        raise ValueError(f"optical OE rungs not ordered: {rungs}")
    return rungs


def check_optical_oe_mount(stated: tuple[float, float, float]) -> list[str]:
    """Mounted ticker MUST restate the axis OE shipment ladder (units)."""
    rungs = load_optical_oe_rungs()
    return [
        f"{tier} optical OE: stated {got:.6g} vs axis {rungs[tier]:.6g}"
        for tier, got in zip(("L", "M", "H"), stated)
        if abs(got - rungs[tier]) / rungs[tier] > 1e-6
    ]


def optical_oe_bom_pool(tier: str) -> float:
    """Whole-OE BoM pool (USD) = oe_tier × $450 — axis §4 reference, not revenue."""
    return load_optical_oe_rungs()[tier] * OPTICAL_OE_ASP_USD


def optical_narrow_rev(
    oe: float, laser_usd_per_oe: float, laser_share: float
) -> float:
    """LITE-style narrow proxy: OE units × laser $/OE × share (axis §4a)."""
    return oe * laser_usd_per_oe * laser_share


def optical_attachment(rev_usd: float, tier: str) -> float:
    """rev / (oe_tier × $450); axis §4a."""
    pool = optical_oe_bom_pool(tier)
    if pool <= 0:
        raise ValueError(f"optical BoM pool for {tier} is non-positive")
    return rev_usd / pool


def check_optical_narrow_attach(
    rev_by_tier: dict[str, float], *, max_attach: float = 1.0
) -> list[str]:
    """Narrow-scope revenue must not exceed the whole-OE BoM pool without a flag.

    Broad (pluggable/ASIC) diagnostics are reported separately in the axis §4b
    and MUST NOT be passed here.
    """
    errors: list[str] = []
    for tier, rev in rev_by_tier.items():
        att = optical_attachment(rev, tier)
        if att > max_attach + 1e-9:
            errors.append(
                f"{tier} optical narrow attach {att:.1%} > {max_attach:.0%} "
                f"(rev ${rev/1e9:.3f}B vs pool ${optical_oe_bom_pool(tier)/1e9:.3f}B) "
                f"— use §4b broad label or cut the numerator"
            )
    return errors


# --- _AXIS.coal-price --------------------------------------------------------
# §1 rung rows: | **L** | 长协 regime 内的实测谷底回归 | **703** | sources | as-of |
# Column 2 (0-based) is the QHD 5500 annual-average coal price every mount reads.
# §1 base series rows: | FY2019 | **588** | main source | cross | diff |
_COAL_ROW = re.compile(r"^\|\s*\*\*([LMH])\*\*\s*\|")
_COAL_SERIES_ROW = re.compile(r"^\|\s*FY(\d{4})\s*\|\s*\*\*([0-9,]+)\*\*")

# Axis §3: the axis is zero-sum and NON-monotonic — the same coal state is a
# miner's good year and a generator's bad one, so the two sides read the rungs
# in opposite order.  A mount that "takes the bad side" on its own would break
# the mirror (both sides assuming they lose).
_COAL_SIDES = {"miner": ("L", "M", "H"), "generator": ("H", "M", "L")}


def load_coal_rungs(path: Path | None = None) -> dict[str, float]:
    """{tier: QHD 5500 CNY/t annual average} from _AXIS.coal-price.md §1."""
    md = (path or AXIS_COAL).read_text(encoding="utf-8")
    rungs: dict[str, float] = {}
    for line in md.splitlines():
        m = _COAL_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or m.group(1) in rungs:
            continue
        # §2 (regime prose) rows share the **L**/**M**/**H** prefix; only the
        # §1 rung table carries a number in column 2, and it comes first.
        try:
            rungs[m.group(1)] = float(cells[2].strip("* ").replace(",", ""))
        except ValueError:
            continue
    missing = {"L", "M", "H"} - rungs.keys()
    if missing:
        raise ValueError(f"coal axis rungs missing: {sorted(missing)}")
    if not (rungs["L"] < rungs["M"] < rungs["H"]):
        raise ValueError(f"coal axis rungs not ordered: {rungs}")
    return rungs


def load_coal_series(path: Path | None = None) -> dict[int, float]:
    """{fiscal year: QHD 5500 annual average} from the axis §1 base series —
    the one series every mount's `coal_fy*` rows must restate (axis 4c-2)."""
    md = (path or AXIS_COAL).read_text(encoding="utf-8")
    series = {
        int(m.group(1)): float(m.group(2).replace(",", ""))
        for m in (_COAL_SERIES_ROW.match(line) for line in md.splitlines())
        if m
    }
    if not series:
        raise ValueError("coal axis base series not found")
    return series


def check_coal_mount(
    stated: tuple[float, float, float], side: str, path: Path | None = None
) -> list[str]:
    """Verify a mount's coal_l/m/h are the axis rungs read from its own side.

    `side` is "miner" (L = axis L = 703) or "generator" (L = axis H = 965):
    a generator's worst world is the miner's best, and the axis §3 mirror is
    what this check enforces — a rung edited on one side turns the other
    side's eval red.
    """
    if side not in _COAL_SIDES:
        raise ValueError(f"side must be one of {sorted(_COAL_SIDES)}, got {side!r}")
    rungs = load_coal_rungs(path)
    return [
        f"{tier} coal rung ({side} reads axis {axis_tier}): stated {got:g} vs axis {rungs[axis_tier]:g}"
        for tier, axis_tier, got in zip(("L", "M", "H"), _COAL_SIDES[side], stated)
        if abs(got - rungs[axis_tier]) > 0.5
    ]


# --- _AXIS.crypto-cycle -------------------------------------------------------
# §1 rung rows: | **L** | 熊 regime | **14** | 待补 | source | as-of |
# Column 2 (0-based) = 2028E adjusted EBITDA in USD 亿 (hundreds of millions).
_CRYPTO_ROW = re.compile(r"^\|\s*\*\*([LMH])\*\*\s*\|")
_CRYPTO_EBITDA_TOL = 0.5  # 亿 USD; rungs are stated as whole 亿


def load_crypto_rungs(path: Path | None = None) -> dict[str, float]:
    """{tier: 2028E adjusted EBITDA in USD 亿} from _AXIS.crypto-cycle.md §1."""
    md = (path or AXIS_CRYPTO).read_text(encoding="utf-8")
    rungs: dict[str, float] = {}
    for line in md.splitlines():
        m = _CRYPTO_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        try:
            rungs[m.group(1)] = float(cells[2].strip("*").replace(",", ""))
        except ValueError:
            continue
    missing = {"L", "M", "H"} - rungs.keys()
    if missing:
        raise ValueError(f"crypto axis rungs missing: {sorted(missing)}")
    if not (rungs["L"] < rungs["M"] < rungs["H"]):
        raise ValueError(f"crypto rungs not ordered: {rungs}")
    return rungs


def check_crypto_mount(
    stated: tuple[float, float, float], path: Path | None = None
) -> list[str]:
    """Full mount (COIN): the ticker's stated rungs must be the axis's own."""
    rungs = load_crypto_rungs(path)
    return [
        f"{tier} crypto EBITDA rung: stated {got:g} vs axis {rungs[tier]:g}"
        for tier, got in zip(("L", "M", "H"), stated)
        if abs(got - rungs[tier]) > _CRYPTO_EBITDA_TOL
    ]


def check_crypto_l_only_mount(
    l_revenue: float, own_trough: float, stated_axis_l: float, path: Path | None = None
) -> list[str]:
    """L-only mount (HOOD, axis §2b): the axis constrains the bear tier alone.

    Two tests, because an L-only mount shares a *constraint*, not a numerator:

    1. The ticker's L-tier crypto revenue must be its own measured trough — the
       observable that keeps the tier honest.  M/H are not axis-driven, so
       there is no rung to bridge them from.
    2. The ticker must restate the axis's L rung even though its formula never
       consumes it.  The shared claim is "this bear is not worse than the axis
       L", which no arithmetic can check; restating the rung is what makes
       editing the axis turn this side red, so the constraint gets re-read
       instead of silently drifting.
    """
    rungs = load_crypto_rungs(path)
    errors: list[str] = []
    if abs(l_revenue - own_trough) > 1.0:
        errors.append(
            f"L crypto revenue {l_revenue:g} is not the own measured trough "
            f"{own_trough:g} (axis §2b L-only mount)"
        )
    if abs(stated_axis_l - rungs["L"]) > _CRYPTO_EBITDA_TOL:
        errors.append(
            f"restated axis L rung {stated_axis_l:g} vs axis {rungs['L']:g} — "
            f"re-read whether this ticker's L tier is still no more bearish "
            f"than the axis L (axis §2b)"
        )
    return errors


# --- _AXIS.rates ----------------------------------------------------------------
# §1 segment rows: | **USD** | **4.00%** | **5.12%** | **4.75%** | **+75bp** | **50bp** | **50bp** | as-of |
# HKD rides USD (linked peg), so it has no numbers of its own.
_RATES_ROW = re.compile(r"^\|\s*\**([A-Z]{3})\**\s*\|")
_RATES_PEG = {"HKD": "USD"}
_RATES_FIELDS = ("rf_mid", "rf_now", "rf_cons", "delta_cons", "rf_step", "rf_cap")


def _rate_cell(cell: str) -> float:
    s = cell.strip().strip("*").replace("+", "")
    if s.endswith("bp"):
        return float(s[:-2]) / 1e4
    if s.endswith("%"):
        return float(s[:-1]) / 100
    raise ValueError(f"rate cell {cell!r} is neither % nor bp")


def load_rates(ccy: str, path: Path | None = None) -> dict[str, float]:
    """Listing-currency segment of _AXIS.rates §1 as decimals.

    Raises on an unbuilt segment (`待建`) rather than defaulting, and when the
    stated Δ_cons disagrees with 共识 − 中枢 — the column is a restatement, so
    editing one cell alone must fail.
    """
    ccy = _RATES_PEG.get(ccy, ccy)
    md = (path or AXIS_RATES).read_text(encoding="utf-8")
    for line in md.splitlines():
        m = _RATES_ROW.match(line)
        if not m or m.group(1) != ccy:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        try:
            seg = dict(zip(_RATES_FIELDS, (_rate_cell(c) for c in cells[1:7])))
        except ValueError as e:
            raise ValueError(f"rates axis {ccy} segment not built: {e}") from None
        if abs(seg["rf_cons"] - seg["rf_mid"] - seg["delta_cons"]) > 5e-5:
            raise ValueError(
                f"rates axis {ccy}: stated Δ_cons {seg['delta_cons']:.4f} ≠ "
                f"共识 {seg['rf_cons']:.4f} − 中枢 {seg['rf_mid']:.4f}"
            )
        return seg
    raise ValueError(f"rates axis has no {ccy} segment")

