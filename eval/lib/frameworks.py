"""Discover tickers and parse L/M/H anchors from frameworks/*.md."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .inputs import parse_baskets

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAMEWORKS_DIR = REPO_ROOT / "frameworks"
EVAL_DIR = REPO_ROOT / "eval"

# Prefer Outputs-style lines: L = **x** · M = **y** · H = **z** (also ≈)
_ANCHOR_BOLD = re.compile(
    r"L\s*[≈=]\s*\*\*([0-9]+(?:\.[0-9]+)?)\*\*"
    r".{0,40}?"
    r"M\s*[≈=]\s*\*\*([0-9]+(?:\.[0-9]+)?)\*\*"
    r".{0,40}?"
    r"H\s*[≈=]\s*\*\*([0-9]+(?:\.[0-9]+)?)\*\*",
    re.IGNORECASE | re.DOTALL,
)

# §0 shorthand: L 27.2 / M 71.2 / H 113.1
_ANCHOR_SLASH = re.compile(
    r"\bL\s*[≈=]?\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*"
    r"M\s*[≈=]?\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*"
    r"H\s*[≈=]?\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

# Option ceiling: `\(V_{opt}\) = **35.25**`, also `**\(V_{opt}\) = +35.25**`
_V_OPT = re.compile(r"V_\{?opt\}?\\?\)?\s*[≈=]\s*\*{0,2}\s*\+?([0-9]+(?:\.[0-9]+)?)")

# Per-tier option credit: `\(p_L/p_M/p_H\) = **0 / 0.25 / 1.0**`
_P_TIERS = re.compile(
    r"p_L\s*/\s*p_M\s*/\s*p_H\\?\)?\s*[≈=]\s*\*{0,2}\s*"
    r"([0-9]*\.?[0-9]+)\s*/\s*([0-9]*\.?[0-9]+)\s*/\s*([0-9]*\.?[0-9]+)"
)

# Consensus-upgrade candidate band (AGENTS.md「共识升级候选带」): the base deck's
# next version previewed before a 待补 input arrives.  Primed anchors keep it out
# of the base L/M/H regexes; the label in （…） is what the digest prints.
#   - 候选带（FY28·Piper单家）：L′ = **7.23** · M′ = **15.54** · H′ = **16.29** · 门槛 A22
_CANDIDATE = re.compile(
    r"候选带（([^）]+)）[^\n]*?"
    r"L′\s*[≈=]\s*\*\*([0-9]+(?:\.[0-9]+)?)\*\*[^\n]*?"
    r"M′\s*[≈=]\s*\*\*([0-9]+(?:\.[0-9]+)?)\*\*[^\n]*?"
    r"H′\s*[≈=]\s*\*\*([0-9]+(?:\.[0-9]+)?)\*\*"
)

# Header line 3 of a framework the rules stopped (valuation-rules.md「BLOCKED 的语义」):
#   > **状态：BLOCKED**（reason; unblock condition）
# The deck line is still written (对照带) so the anchors parse; consumers
# (digest / index / calibration) must not derive a zone from it.
_BLOCKED_LINE = re.compile(r"^>\s*\*\*状态：BLOCKED\*\*", re.MULTILINE)
# Reason runs to end of line so nested parens (e.g. `③ 出带（M 7.0× < 10.4×）`) are kept whole.
_BLOCKED = re.compile(r"^>\s*\*\*状态：BLOCKED\*\*\s*[（(](.+?)[）)]\s*$", re.MULTILINE)

_CURRENCY = re.compile(r"（(USD|HKD|CNY|TWD)[^）]*）|\((USD|HKD|CNY|TWD)[^)]*\)")
# 中文写法也认，否则「（人民币）」会退回全文扫描并被无关括注劫持
_CURRENCY_CN = {"人民币": "CNY", "港元": "HKD", "港币": "HKD", "美元": "USD", "新台币": "TWD"}
# 文件名后缀兜底：市场决定计价币种
_SUFFIX_CCY = {".HK": "HKD", ".US": "USD", ".SH": "CNY", ".SZ": "CNY", ".SS": "CNY"}
_TITLE = re.compile(r"^#\s+(\S+)\s+[—–-]\s+(.+)$", re.MULTILINE)
_EVAL_PTR = re.compile(r"eval:\s*(eval/\S+\.py)")
_LYNCH = re.compile(
    r"林奇：\s*`([a-z_]+)`(?:\s*/\s*`([a-z_]+)`)?",
    re.IGNORECASE,
)

# AGENTS.md / sediment-framework 六类中英对照
LYNCH_CN: dict[str, str] = {
    "slow_grower": "缓慢增长",
    "stalwart": "绩优",
    "fast_grower": "快速增长",
    "cyclical": "周期",
    "turnaround": "转机",
    "asset_play": "资产隐蔽",
}


def basket_label(meta: "FrameworkMeta") -> str:
    """Human label for the index / digest: `篮 4 10%` when every leg shares one
    basket (e.g. A/H companion rows), per leg when the baskets differ."""
    from .gordon import BASKET_R  # local import: gordon does not depend on frameworks

    if not meta.baskets:
        return "—"
    cells = [f"篮 {b} {BASKET_R[b][1]:.1%}".replace(".0%", "%") for b in meta.baskets.values()]
    if len(set(meta.baskets.values())) == 1:
        return cells[0]
    return " · ".join(f"{k} {c}" for k, c in zip(meta.baskets, cells))


def lynch_cn(primary: str, secondary: str | None = None) -> str:
    """Human-facing Lynch label; unknown enums fall back to raw id."""
    p = LYNCH_CN.get(primary, primary)
    if secondary:
        return f"{p}/{LYNCH_CN.get(secondary, secondary)}"
    return p


@dataclass(frozen=True)
class FrameworkMeta:
    ticker: str
    path: Path
    name: str
    L: float
    M: float
    H: float
    currency: str
    eval_script: Path | None
    lynch_primary: str
    lynch_secondary: str | None
    v_opt: float | None = None
    p_tiers: tuple[float, float, float] | None = None
    # Not an option: no p_k, never feeds the base zone (AGENTS.md「共识升级候选带」).
    candidate: tuple[float, float, float] | None = None
    candidate_label: str | None = None
    # AGENTS.md「BLOCKED — 题头契约」: reason text when the rules stopped this ticker.
    blocked: str | None = None
    # {r-field: basket} from the Inputs `篮 N` tags (AGENTS.md「篮子声明」).
    baskets: dict[str, int] = field(default_factory=dict)

    @property
    def option_band(self) -> tuple[float, float, float] | None:
        """L/M/H with per-tier option credit, or None if this ticker has no option.

        Needs both V_opt and all three p_k: a ceiling without the per-tier
        credit says nothing about where the corrected band sits.
        """
        if self.v_opt is None or self.p_tiers is None:
            return None
        p_l, p_m, p_h = self.p_tiers
        return (
            self.L + p_l * self.v_opt,
            self.M + p_m * self.v_opt,
            self.H + p_h * self.v_opt,
        )

    @property
    def lynch_label(self) -> str:
        """English enum id(s), for machine use."""
        if self.lynch_secondary:
            return f"{self.lynch_primary}/{self.lynch_secondary}"
        return self.lynch_primary

    @property
    def lynch_label_cn(self) -> str:
        """中文林奇类，供飞书等人读消息。"""
        return lynch_cn(self.lynch_primary, self.lynch_secondary)


# Market section order shared by _INDEX.md and the daily digest.
MARKET_ORDER: tuple[str, ...] = ("US", "HK", "A")


def market_bucket(ticker: str) -> str | None:
    if ticker.endswith(".US"):
        return "US"
    if ticker.endswith(".HK"):
        return "HK"
    if ticker.endswith((".SH", ".SZ")):
        return "A"
    return None


def _market_sort_key(p: Path) -> tuple[int, str]:
    bucket = market_bucket(p.stem)
    rank = MARKET_ORDER.index(bucket) if bucket in MARKET_ORDER else len(MARKET_ORDER)
    return rank, p.name


def list_framework_paths(frameworks_dir: Path | None = None) -> list[Path]:
    """Framework files grouped US → HK → A (same as _INDEX.md), name-sorted within.

    Plain name sort interleaves HK and A tickers by leading digit, which reads
    as random in the digest.
    """
    d = frameworks_dir or FRAMEWORKS_DIR
    out: list[Path] = []
    for p in sorted(d.glob("*.md"), key=_market_sort_key):
        if p.name.startswith("_"):
            continue
        out.append(p)
    return out


def _parse_anchors(md: str) -> tuple[float, float, float]:
    # Prefer ### Outputs section if present
    outputs = md
    m_out = re.search(r"^### Outputs\b.*", md, re.MULTILINE)
    if m_out:
        outputs = md[m_out.start() :]
        nxt = re.search(r"^## \d", outputs[1:], re.MULTILINE)
        if nxt:
            outputs = outputs[: nxt.start() + 1]

    # An Outputs section carrying two L/M/H triples (e.g. a sensitivity row) is
    # ambiguous: silently taking one of them is how the daily digest would
    # broadcast the wrong band. Refuse instead.
    for block, where in ((outputs, "### Outputs"), (md, "whole document")):
        for rx in (_ANCHOR_BOLD, _ANCHOR_SLASH):
            matches = list(rx.finditer(block))
            if len(matches) > 1:
                found = ", ".join(f"{m.group(1)}/{m.group(2)}/{m.group(3)}" for m in matches)
                raise ValueError(
                    f"ambiguous L/M/H anchors in {where}: {len(matches)} triples ({found})"
                )
            if matches:
                g = matches[0]
                return float(g.group(1)), float(g.group(2)), float(g.group(3))
    raise ValueError("cannot parse L/M/H anchors")


def _parse_option(md: str) -> tuple[float | None, tuple[float, float, float] | None]:
    """V_opt and the three per-tier credits; both absent is the common case."""
    vm = _V_OPT.search(md)
    pm = _P_TIERS.search(md)
    v_opt = float(vm.group(1)) if vm else None
    p_tiers = (
        (float(pm.group(1)), float(pm.group(2)), float(pm.group(3))) if pm else None
    )
    return v_opt, p_tiers


def _parse_candidate(md: str) -> tuple[str, tuple[float, float, float]] | None:
    """(label, (L′, M′, H′)) or None.  Two candidate lines are as ambiguous as two decks."""
    matches = list(_CANDIDATE.finditer(md))
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"ambiguous candidate bands: {len(matches)} 候选带 lines")
    m = matches[0]
    band = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
    if not (band[0] < band[1] < band[2]):
        raise ValueError(f"candidate band needs L′ < M′ < H′, got {band}")
    return m.group(1).strip(), band


def _parse_blocked(md: str, path: Path) -> str | None:
    """BLOCKED reason, or None when the header line is absent.

    A BLOCKED line with no reason must fail the parse rather than fall back to
    "not blocked": a falsy reason would let the digest derive a zone the header
    forbids (AGENTS.md「BLOCKED — 题头契约」).
    """
    if not _BLOCKED_LINE.search(md):
        return None
    m = _BLOCKED.search(md)
    reason = m.group(1).strip() if m else ""
    if not reason:
        raise ValueError(
            f"{path.name}: BLOCKED header needs a reason — "
            "`> **状态：BLOCKED**（reason；unblock condition）`"
        )
    return reason


def _parse_currency(md: str) -> str:
    """Currency of the anchors. Never guesses USD — an unlabelled band is an error."""
    tm = re.search(r"^#\s+(\S+)", md, re.M)
    ticker = tm.group(1) if tm else ""

    # 1) the 价带锚点 / 五档区间 line is the authoritative label
    for line in md.splitlines():
        if "价带锚点" in line or "本票价格区间" in line:
            m = _CURRENCY.search(line)
            if m:
                return m.group(1) or m.group(2)
            for cn, code in _CURRENCY_CN.items():
                if cn in line:
                    return code

    # 2) market suffix — a Shanghai listing is priced in CNY whatever the prose says
    for suffix, code in _SUFFIX_CCY.items():
        if ticker.endswith(suffix):
            return code

    raise ValueError(
        f"{ticker or md[:40]!r}: cannot determine currency — label the 价带锚点 line "
        f"(e.g. 「价带锚点（CNY）」) or use a market-suffixed ticker"
    )


def load_framework(path: Path) -> FrameworkMeta:
    md = path.read_text(encoding="utf-8")
    tm = _TITLE.search(md)
    if not tm:
        raise ValueError(f"{path.name}: missing `# TICKER — name` title")
    ticker, name = tm.group(1), tm.group(2).strip()
    if path.stem != ticker:
        raise ValueError(f"{path.name}: stem {path.stem!r} != title ticker {ticker!r}")
    L, M, H = _parse_anchors(md)
    if not (L < M < H):
        raise ValueError(f"{ticker}: need L < M < H, got {L}, {M}, {H}")
    v_opt, p_tiers = _parse_option(md)
    candidate = _parse_candidate(md)
    if p_tiers is not None and v_opt is None:
        raise ValueError(f"{ticker}: p_L/p_M/p_H given without V_opt")
    if p_tiers is not None and not all(0.0 <= p <= 1.0 for p in p_tiers):
        raise ValueError(f"{ticker}: p_L/p_M/p_H out of [0,1], got {p_tiers}")
    currency = _parse_currency(md)
    blocked = _parse_blocked(md, path)
    baskets = parse_baskets(md)
    lm = _LYNCH.search(md)
    if not lm:
        raise ValueError(f"{ticker}: missing 林奇：`primary` header")
    lynch_primary = lm.group(1)
    lynch_secondary = lm.group(2)
    ev: Path | None = None
    em = _EVAL_PTR.search(md)
    if em:
        cand = REPO_ROOT / em.group(1)
        if cand.is_file():
            ev = cand
    else:
        cand = EVAL_DIR / f"{ticker}.py"
        if cand.is_file():
            ev = cand
    return FrameworkMeta(
        ticker=ticker,
        path=path,
        name=name,
        L=L,
        M=M,
        H=H,
        currency=currency,
        eval_script=ev,
        lynch_primary=lynch_primary,
        lynch_secondary=lynch_secondary,
        v_opt=v_opt,
        p_tiers=p_tiers,
        candidate=candidate[1] if candidate else None,
        candidate_label=candidate[0] if candidate else None,
        blocked=blocked,
        baskets=baskets,
    )


def load_all_frameworks(frameworks_dir: Path | None = None) -> list[FrameworkMeta]:
    out: list[FrameworkMeta] = []
    errors: list[str] = []
    for p in list_framework_paths(frameworks_dir):
        try:
            out.append(load_framework(p))
        except Exception as e:  # noqa: BLE001 — collect all parse failures
            errors.append(f"{p.name}: {e}")
    if errors:
        raise RuntimeError("framework parse failed:\n- " + "\n- ".join(errors))
    return out
