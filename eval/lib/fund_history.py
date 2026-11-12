"""Validate explicit fund reference and historical-series inputs offline.

Validation checks declared identity, date, units and structure; it does not
authenticate a source or constitute the user's confirmation.
"""
from datetime import date
from math import isfinite


def _date(value):
    if not isinstance(value, str):
        raise ValueError('observation date must be an ISO date')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('observation date must be an ISO date') from exc
    if parsed.isoformat() != value:
        raise ValueError('observation date must be a canonical ISO date')
    return parsed


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError('observation value must be finite')
    if positive and value <= 0:
        raise ValueError('observation value must be positive')
    return value


def _source(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('source reference is required')
    return value


def normalize_fund_reference(observation, security, index_date):
    """Return transparent flags; a historical close proxy remains usable."""
    if not isinstance(observation, dict):
        raise ValueError('fund reference observation is required')
    if observation.get('security') != security or observation.get('unit') != 'CNY_PER_UNIT':
        raise ValueError('fund observation identity or unit is incorrect')
    kind = observation.get('kind')
    if kind not in ('exchange_close_proxy', 'fund_nav'):
        raise ValueError('fund observation kind is unsupported')
    _number(observation.get('value'), positive=True)
    observed, index = _date(observation.get('date')), _date(index_date)
    source = _source(observation.get('source_ref'))
    if kind == 'fund_nav' and source == 'legacy_close_proxy':
        raise ValueError('source cannot relabel the historical close proxy as NAV')
    if kind == 'fund_nav':
        evidence = observation.get('evidence')
        if not isinstance(evidence, dict):
            raise ValueError('fund NAV requires a complete evidence observation')
        _source(evidence.get('provider'))
        _source(evidence.get('document_ref'))
        evidence_date = _date(evidence.get('date'))
        evidence_value = _number(evidence.get('value'), positive=True)
        if (evidence.get('security') != security or evidence.get('unit') != observation['unit']
                or evidence_date != observed or evidence_value != observation['value']):
            raise ValueError('fund NAV evidence does not match its dated observation')
    flags = ['fund_reference_source_requires_review']
    if kind == 'exchange_close_proxy':
        flags.append('fund_reference_is_exchange_close_proxy')
    if observed != index:
        flags.append('fund_reference_and_index_dates_differ')
    return flags


def normalize_histories(snapshot, index_instrument, rate_instrument):
    """Return sorted date-to-value copies of explicitly provided histories."""
    if not isinstance(snapshot, dict):
        raise ValueError('external history snapshot is required')
    def series(name, expected_instrument, unit, positive):
        item = snapshot.get(name)
        if not isinstance(item, dict) or item.get('instrument') != expected_instrument or item.get('unit') != unit:
            raise ValueError(f'{name} identity or unit is incorrect')
        _source(item.get('source_ref'))
        values = item.get('observations')
        if not isinstance(values, dict) or len(values) < 2:
            raise ValueError(f'{name} requires at least two observations')
        for key, value in values.items():
            _date(key)
            _number(value, positive=positive)
        return dict(sorted(values.items()))
    index = series('index_history', index_instrument, 'INDEX_POINTS', True)
    rates = series('rates_history', rate_instrument, 'PERCENT', False)
    if min(rates) > min(index) or max(rates) < max(index):
        raise ValueError('rates history does not cover the index history')
    return index, rates
