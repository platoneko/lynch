"""Five-tier zone mapping from AGENTS.md (L < M < H midpoints)."""
from __future__ import annotations

ZONE_CN: dict[str, str] = {
    "deep_discount": "深度折价",
    "conservative": "保守",
    "neutral": "中性",
    "optimistic": "乐观",
    "stretched": "叙事溢价",
}

# Traffic-light for Feishu digest (matches user mock: 深折绿 / 中性黄 / 叙事红)
ZONE_EMOJI: dict[str, str] = {
    "deep_discount": "🟢",
    "conservative": "🟢",
    "neutral": "🟡",
    "optimistic": "🟠",
    "stretched": "🔴",
}

ZONE_ORDER = (
    "deep_discount",
    "conservative",
    "neutral",
    "optimistic",
    "stretched",
)


def cut_points(L: float, M: float, H: float) -> tuple[float, float]:
    """A=(L+M)/2, B=(M+H)/2."""
    return (L + M) / 2.0, (M + H) / 2.0


def zone_of(price: float, L: float, M: float, H: float) -> str:
    """Map price to zone id. Half-open except optimistic includes H."""
    if not (L < M < H):
        raise ValueError(f"need L < M < H, got {L}, {M}, {H}")
    A, B = cut_points(L, M, H)
    if price < L:
        return "deep_discount"
    if price < A:
        return "conservative"
    if price < B:
        return "neutral"
    if price <= H:
        return "optimistic"
    return "stretched"


def zone_bound_edges(
    zone_id: str, L: float, M: float, H: float
) -> tuple[str, float | None, float | None]:
    """Return (shape, lo, hi) matching zone_of half-open semantics.

    Shapes:
      open_below  — price < hi          (deep_discount; lo is None)
      half_open   — lo ≤ price < hi     (conservative / neutral)
      closed      — lo ≤ price ≤ hi     (optimistic)
      open_above  — lo < price          (stretched; hi is None)
    """
    if not (L < M < H):
        raise ValueError(f"need L < M < H, got {L}, {M}, {H}")
    A, B = cut_points(L, M, H)
    if zone_id == "deep_discount":
        return "open_below", None, L
    if zone_id == "conservative":
        return "half_open", L, A
    if zone_id == "neutral":
        return "half_open", A, B
    if zone_id == "optimistic":
        return "closed", B, H
    if zone_id == "stretched":
        return "open_above", H, None
    raise ValueError(f"unknown zone_id: {zone_id}")


def zone_label(zone_id: str) -> str:
    return ZONE_CN.get(zone_id, zone_id)


def zone_emoji(zone_id: str) -> str:
    return ZONE_EMOJI.get(zone_id, "⚪")

