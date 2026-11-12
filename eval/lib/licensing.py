"""Bind a caller-supplied licensing ladder without reading framework prose."""
import math
import hashlib
import json

LADDER_ID = 'patent_licensing_annuity_evebit'
LADDER_MEASURE = 'pre_tax_ev_ebit'
LADDER_BUSINESSES = {'QCOM.US': 'qtl', 'NOK.US': 'technology_standards'}

def bind_ladder(inputs, shared, ticker):
    if (not isinstance(shared, dict) or set(shared) != {'id', 'version', 'measure', 'businesses', 'tiers'}
        or shared['id'] != LADDER_ID or not isinstance(shared['version'], str) or not shared['version'].strip()
        or shared['measure'] != LADDER_MEASURE or shared['businesses'] != LADDER_BUSINESSES
        or ticker not in LADDER_BUSINESSES):
        raise ValueError('shared licensing definition or business scope is incompatible')
    tiers = shared['tiers']
    if not isinstance(tiers, dict) or set(tiers) != {'l', 'm', 'h'}:
        raise ValueError('shared licensing ladder requires l, m and h')
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in tiers.values()):
        raise ValueError('shared licensing ladder must contain finite numbers')
    if not 0 < tiers['l'] < tiers['m'] < tiers['h']:
        raise ValueError('shared licensing ladder must be positive and strictly increasing')
    bound = dict(inputs)
    for tier in 'lmh':
        key = f'ev_ebit_ts_{tier}'
        if key in bound:
            value = bound[key]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or abs(value - tiers[tier]) > 1e-9):
                raise ValueError(f'shared licensing ladder drift: {key}')
        bound[key] = tiers[tier]
    return bound


def fingerprint(*objects):
    data = json.dumps(objects, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def normalized_rate_readings(rate):
    """Map the existing rate results to the shared price-band format."""
    if rate is None:
        return None
    bands = {key: dict(zip('LMH', rate[key])) for key in ('low', 'high', 'consensus', 'pressure') if key in rate}
    return {'inputs': dict(rate['inputs']), **bands, 'price_currency': 'USD', 'price_unit': 'per_share'}


def rate_readings(rate_inputs, band_at, base):
    def checked_band(shift):
        try:
            values = band_at(shift)
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError('invalid rate scenario') from exc
        if (len(values) != 3 or any(not math.isfinite(value) or value < 0 for value in values)
            or not values[0] < values[1] < values[2]):
            raise ValueError('rate scenario must produce finite, non-negative, increasing price anchors')
        return values

    if any(abs(actual - expected) > 1e-9 for actual, expected in zip(checked_band(0), base)):
        raise ValueError('zero rate shift does not reproduce anchors')
    if rate_inputs is None:
        return None
    keys = ('rf_mid', 'rf_now', 'rf_cons', 'delta_cons', 'rf_step', 'rf_cap')
    if not isinstance(rate_inputs, dict) or any(key not in rate_inputs for key in keys):
        raise ValueError('rate inputs are incomplete')
    if any(isinstance(rate_inputs[key], bool) or not isinstance(rate_inputs[key], (int, float))
           or not math.isfinite(rate_inputs[key]) for key in keys):
        raise ValueError('rate inputs must be finite numbers')
    if rate_inputs['rf_step'] < 0 or rate_inputs['rf_cap'] < 0:
        raise ValueError('rate step and pressure cap must be non-negative')
    if abs(rate_inputs['rf_cons'] - rate_inputs['rf_mid'] - rate_inputs['delta_cons']) > 5e-5:
        raise ValueError('rate consensus delta is inconsistent')
    step, cap, delta = (rate_inputs[key] for key in ('rf_step', 'rf_cap', 'delta_cons'))
    low, high, consensus = checked_band(-step), checked_band(step), checked_band(delta)
    pressure = (checked_band(cap)[0], base[1], checked_band(-cap)[2])
    if not pressure[0] < pressure[1] < pressure[2]:
        raise ValueError('rate pressure must produce increasing price anchors')
    return {'inputs': dict(rate_inputs), 'low': low, 'high': high,
            'consensus': consensus, 'pressure': pressure}
