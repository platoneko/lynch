"""Discover tickers and parse L/M/H anchors from frameworks/*.md."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

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


def list_framework_paths(frameworks_dir: Path | None = None) -> list[Path]:
    d = frameworks_dir or FRAMEWORKS_DIR
    out: list[Path] = []
    for p in sorted(d.glob("*.md")):
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
    if p_tiers is not None and v_opt is None:
        raise ValueError(f"{ticker}: p_L/p_M/p_H given without V_opt")
    if p_tiers is not None and not all(0.0 <= p <= 1.0 for p in p_tiers):
        raise ValueError(f"{ticker}: p_L/p_M/p_H out of [0,1], got {p_tiers}")
    currency = _parse_currency(md)
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
