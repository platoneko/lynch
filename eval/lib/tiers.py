"""Tier-construction guards shared by eval scripts (skill sediment-framework A2e).

L/M/H are supposed to be three *worlds*, told apart by their numerators. When a
tier's numerator sits on top of a neighbour's, the multiple silently takes over
the job of separating them — and a higher multiple on higher earnings is the
classic cycle-peak error. These checks make that visible mechanically instead of
relying on a reviewer to notice.
"""
from __future__ import annotations


def decompose(
    anchors: tuple[float, float, float], pes: tuple[float, float, float]
) -> dict[str, float]:
    """Split L/M and H/M into their numerator and multiple components.

    L/M = (eps_L/eps_M) x (pe_L/pe_M), so comparing raw L/M across tickers
    compares the authors' multiple habits as much as their scenarios (A2e-5).
    """
    L, M, H = anchors
    pe_l, pe_m, pe_h = pes
    return {
        "lm": L / M,
        "hm": H / M,
        "pe_lm": pe_l / pe_m,
        "pe_hm": pe_h / pe_m,
        "num_lm": (L / M) / (pe_l / pe_m),
        "num_hm": (H / M) / (pe_h / pe_m),
    }


def flat_multiple_ladder(
    eps: tuple[float, float, float], pe_m: float
) -> tuple[list[str], tuple[float, float, float]]:
    """A2e-2c: re-price all three tiers at pe_m; the ladder must survive.

    Returns (errors, flat anchors). A tier that collapses into a neighbour once
    the multiple is held flat has no independent numerator, so the framework has
    to reset that tier's scenario rather than prop it up with a multiple.
    """
    eps_l, eps_m, eps_h = eps
    errors: list[str] = []
    if not (eps_l < eps_m < eps_h):
        errors.append(
            f"numerator ladder broken: eps {eps_l:.3f}/{eps_m:.3f}/{eps_h:.3f} "
            f"— tiers must differ in the numerator, not only in the multiple (A2e-2c)"
        )
    return errors, (eps_l * pe_m, eps_m * pe_m, eps_h * pe_m)


def report(
    anchors: tuple[float, float, float],
    pes: tuple[float, float, float],
    eps: tuple[float, float, float],
) -> list[str]:
    """Printable A2e-5/2c block: decomposition plus the flat-multiple ladder."""
    d = decompose(anchors, pes)
    _, flat = flat_multiple_ladder(eps, pes[1])
    return [
        f"tier decomposition (A2e-5):"
        f"  L/M {d['lm']:.2f} = pe {d['pe_lm']:.2f} x num {d['num_lm']:.2f}"
        f"  |  H/M {d['hm']:.2f} = pe {d['pe_hm']:.2f} x num {d['num_hm']:.2f}",
        f"flat-multiple ladder (A2e-2c, all tiers at pe_m={pes[1]:g}):"
        f"  {flat[0]:.2f}/{flat[1]:.2f}/{flat[2]:.2f}",
    ]
