"""Compare an explicit local input snapshot; never assert full confirmation."""
import hashlib
import json
import re


def _normalize_numbers(value):
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_numbers(item) for item in value]
    # JSON distinguishes 1 and 1.0 in text, but they are the same value.
    # Keep booleans separate: bool is a subclass of int in Python.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def local_fingerprint(payload) -> str:
    """Return a deterministic digest of the caller-selected local facts."""
    if not isinstance(payload, dict):
        raise ValueError('local basis must be a mapping')
    try:
        encoded = json.dumps(_normalize_numbers(payload), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('local basis must contain finite JSON data') from exc
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def local_basis_changed(payload, baseline: str) -> bool:
    """Compare only this local snapshot, not unrecorded inputs or approval."""
    if not isinstance(baseline, str) or not re.fullmatch(r'[0-9a-f]{64}', baseline):
        raise ValueError('local basis baseline must be a SHA-256 digest')
    return local_fingerprint(payload) != baseline
