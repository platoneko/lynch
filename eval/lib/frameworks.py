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

_CURRENCY = re.compile(r"（(USD|HKD|CNY|TWD)[^）]*）|\((USD|HKD|CNY|TWD)[^)]*\)")
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

    for block in (outputs, md):
        matches = list(_ANCHOR_BOLD.finditer(block))
        if matches:
            g = matches[-1]
            return float(g.group(1)), float(g.group(2)), float(g.group(3))
        matches = list(_ANCHOR_SLASH.finditer(block))
        if matches:
            g = matches[-1]
            return float(g.group(1)), float(g.group(2)), float(g.group(3))
    raise ValueError("cannot parse L/M/H anchors")


def _parse_currency(md: str) -> str:
    # Prefer 价带锚点 line
    for line in md.splitlines():
        if "价带锚点" in line or "本票价格区间（USD）" in line:
            m = _CURRENCY.search(line)
            if m:
                return m.group(1) or m.group(2)
    m = _CURRENCY.search(md)
    if m:
        return m.group(1) or m.group(2)
    if ".HK" in (re.search(r"^#\s+(\S+)", md, re.M).group(1) if re.search(r"^#\s+(\S+)", md, re.M) else ""):
        return "HKD"
    if ".US" in (re.search(r"^#\s+(\S+)", md, re.M).group(1) if re.search(r"^#\s+(\S+)", md, re.M) else ""):
        return "USD"
    return "USD"


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
