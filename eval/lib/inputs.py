"""Parse a framework's §3 Inputs table and its stated Outputs.

Every eval script re-derives L/M/H from the framework's own Inputs table and
compares the result against the anchors the framework states.  That comparison
is boilerplate; the per-ticker formula is not.  This module holds the
boilerplate so a new eval only has to spell out its formula and its guards.

Existing eval scripts each carry their own copy of `parse_inputs` — they are
left alone on purpose (touching 19 unrelated files is not part of any one
ticker's task).  New evals should import from here.
"""

from __future__ import annotations

import re
import sys

# Cells that are headers, separators, or explicitly "not yet sourced".
_SKIP_KEYS = {"字段名", "--------", "---"}
_SKIP_VALUES = {"取值", "待补", "", "—", "-"}

_STATED = {
    "L": re.compile(r"L = \*\*([0-9.]+)\*\*"),
    "M": re.compile(r"M = \*\*([0-9.]+)\*\*"),
    "H": re.compile(r"H = \*\*([0-9.]+)\*\*"),
    "A": re.compile(r"A = \*\*([0-9.]+)\*\*"),
    "B": re.compile(r"B = \*\*([0-9.]+)\*\*"),
}
_STATED_ZONE = re.compile(r"zone = \*\*`([a-z_]+)`\*\*")


def parse_inputs(md: str) -> dict[str, float]:
    """Read the `### Inputs` markdown table into {field: float}.

    Rows whose value is `待补` are skipped rather than defaulted — a missing
    input must surface as a missing key, not as a silent zero.
    """
    out: dict[str, float] = {}
    in_inputs = False
    for line in md.splitlines():
        if line.startswith("### Inputs"):
            in_inputs = True
            continue
        if in_inputs and line.startswith("### "):
            break
        if not in_inputs or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        key, raw = cells[0], cells[1]
        if key in _SKIP_KEYS or raw in _SKIP_VALUES:
            continue
        try:
            out[key] = float(raw.replace(",", ""))
        except ValueError:
            continue
    return out


def outputs_section(md: str) -> str:
    """The `### Outputs` slice, so stated anchors are not matched elsewhere."""
    return md.split("### Outputs", 1)[-1] if "### Outputs" in md else md


def stated_anchors(md: str) -> dict[str, float | None]:
    sec = outputs_section(md)
    return {k: (float(m.group(1)) if (m := pat.search(sec)) else None)
            for k, pat in _STATED.items()}


def stated_zone(md: str) -> str | None:
    m = _STATED_ZONE.search(outputs_section(md))
    return m.group(1) if m else None


def check_missing(inp: dict[str, float], required: list[str]) -> list[str]:
    missing = [k for k in required if k not in inp]
    return [f"missing inputs {missing}"] if missing else []


def check_against_stated(
    md: str,
    computed: dict[str, float],
    zone: str,
    tol: float = 0.05,
) -> list[str]:
    """Compare recomputed anchors/cut-points/zone with what the framework says."""
    errors: list[str] = []
    stated = stated_anchors(md)
    for key, value in computed.items():
        said = stated.get(key)
        if said is None:
            errors.append(f"stated {key} not found in Outputs")
        elif abs(said - value) > tol:
            errors.append(f"{key}: stated {said} vs recomputed {value:.4f}")
    said_zone = stated_zone(md)
    if said_zone != zone:
        errors.append(f"zone: stated {said_zone} vs recomputed {zone}")
    return errors


def ladder(name: str, *values: float) -> list[str]:
    """Assert a strictly increasing ladder; returns [] or one error."""
    if all(a < b for a, b in zip(values, values[1:])):
        return []
    return [f"{name} ladder violated: {' < '.join(f'{v:g}' for v in values)} is false"]


def reverse_ladder(name: str, *values: float) -> list[str]:
    """Assert a strictly decreasing ladder (cyclical reverse-multiple policy)."""
    if all(a > b for a, b in zip(values, values[1:])):
        return []
    return [f"{name} reverse ladder violated: {' > '.join(f'{v:g}' for v in values)} is false"]


def report(errors: list[str], label: str = "FAIL") -> int:
    if errors:
        print(f"{label}:", "; ".join(errors), file=sys.stderr)
        return 1
    print("OK")
    return 0
