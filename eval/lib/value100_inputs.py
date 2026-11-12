"""Explicit historical index data and optional peer bands for value-100 ETF."""
from __future__ import annotations

from datetime import date
from math import isfinite


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError('observation must be a finite number')
    if positive and value <= 0:
        raise ValueError('observation must be positive')
    return value


def normalize_index_history(snapshot, instrument):
    """Use only caller-provided index observations; no cache or rate series."""
    if not isinstance(snapshot, dict):
        raise ValueError('external index history is required')
    item = snapshot.get('index_history')
    if not isinstance(item, dict) or item.get('instrument') != instrument or item.get('unit') != 'INDEX_POINTS':
        raise ValueError('index history identity or unit is incorrect')
    if not isinstance(item.get('source_ref'), str) or not item['source_ref'].strip():
        raise ValueError('index history source is required')
    values = item.get('observations')
    if not isinstance(values, dict) or len(values) < 2:
        raise ValueError('index history needs at least two observations')
    for key, value in values.items():
        if not isinstance(key, str):
            raise ValueError('index dates must be canonical ISO dates')
        try:
            if date.fromisoformat(key).isoformat() != key:
                raise ValueError('index dates must be canonical ISO dates')
        except ValueError as exc:
            raise ValueError('index dates must be canonical ISO dates') from exc
        _number(value, positive=True)
    return dict(sorted(values.items()))


def select_peer_bands(peers, expected):
    """Return usable CNY-per-share peers and disclosure flags without guessing."""
    if not isinstance(peers, dict) or not isinstance(expected, dict):
        raise ValueError('peer snapshots and declarations must be mappings')
    bands, flags = {}, []
    for alias, ticker in expected.items():
        value = peers.get(alias)
        if value is None:
            flags.append(f'peer_{alias}_missing')
            continue
        try:
            if (not isinstance(value, dict) or value.get('ticker') != ticker
                    or value.get('currency') != 'CNY' or value.get('units', {}).get('price') != 'CNY_PER_SHARE'):
                raise ValueError('peer identity or price unit differs')
            price = value['anchors']
            if not isinstance(price, dict) or set(price) != {'L', 'M', 'H'}:
                raise ValueError('peer price tiers missing')
            low, mid, high = (_number(price[t], positive=True) for t in 'LMH')
            if not low < mid < high:
                raise ValueError('peer tiers not increasing')
        except (KeyError, TypeError, ValueError):
            flags.append(f'peer_{alias}_invalid')
            continue
        bands[alias] = dict(price)
        # Runner peer snapshots are unconfirmed calculation outputs. A field
        # inside the output cannot prove user or operator acceptance; a future
        # result-service adapter must supply and verify independent evidence.
        flags.append(f'peer_{alias}_unconfirmed')
    return bands, flags
