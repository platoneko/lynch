"""Preserve a published historical approximation while propagating changes."""
from math import isfinite


def preserve_reference_residual(current_unrounded: float, historical_unrounded: float,
                                historical_adopted: float) -> float:
    """Carry the historical adoption-vs-recalculation residual unchanged.

    The historical inputs and adopted value must be documented and frozen by
    the caller. This is arithmetic alignment, not evidence that the original
    approximation or any new forecast is correct.
    """
    values = (current_unrounded, historical_unrounded, historical_adopted)
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not isfinite(x) for x in values):
        raise ValueError('historical precision inputs must be finite numbers')
    return historical_adopted + (current_unrounded - historical_unrounded)


def historical_oci_reference(reference: dict, historical_aws_turnover: float) -> dict:
    """Reconstruct ORCL's frozen 2026-10-19 unrounded reference inputs.

    This historical bridge is intentionally independent of a stock's current
    software formula and current AWS turnover. Its formula and the supplied
    original inputs document the historical precision adjustment; changes to
    either require explicit review of that historical reference.
    """
    if not isinstance(reference, dict):
        raise ValueError('historical OCI reference must be a mapping')
    if (isinstance(historical_aws_turnover, bool) or not isinstance(historical_aws_turnover, (int, float))
            or not isfinite(historical_aws_turnover) or historical_aws_turnover <= 0):
        raise ValueError('historical AWS turnover must be positive and finite')
    fields = ('tax_rate', 'shares_fy28', 'sbc_fy28', 'int_fy28', 'eps_fy28_low', 'eps_fy28_mid',
              'rev_software_fy26', 'rev_saas_fy26', 'rev_hw_fy26', 'rev_svc_fy26',
              'g_software', 'g_saas', 'g_hw', 'g_svc', 'oci_rev_fy26', 'oci_gm_est',
              'sm_total_fy26', 'oci_sm_share', 'seg_margin_fy26', 'below_seg_fy26',
              'sw_share_below', 'g_below', 'oci_ppe_now', 'capex_rest_fy27',
              'da_rest_fy27', 'rpo', 'rev_fy28_mid')
    if any(key not in reference for key in fields):
        raise ValueError('historical OCI reference is missing fields')
    if any(isinstance(reference[key], bool) or not isinstance(reference[key], (int, float))
           or not isfinite(reference[key]) for key in fields):
        raise ValueError('historical OCI reference inputs must be finite')
    # Historical leg composition is frozen here, not taken from current code.
    legs = ('software', 'saas', 'hw', 'svc')
    revenue_26 = sum(reference[f'rev_{leg}_fy26'] for leg in legs)
    revenue_28 = sum(reference[f'rev_{leg}_fy26'] * (1 + reference[f'g_{leg}']) ** 2 for leg in legs)
    oci_contrib = (reference['oci_rev_fy26'] * reference['oci_gm_est']
                   - reference['sm_total_fy26'] * reference['oci_sm_share'])
    margin = (reference['seg_margin_fy26'] - oci_contrib) / revenue_26
    ebit = (margin * revenue_28 - reference['below_seg_fy26'] * reference['sw_share_below']
            * (1 + reference['g_below']) ** 2)
    keep = 1 - reference['tax_rate']
    software_nopat = ebit * keep
    spent = reference['oci_ppe_now'] + reference['capex_rest_fy27'] - reference['da_rest_fy27']
    oci_revenue_mid = reference['rev_fy28_mid'] - revenue_28
    incremental_revenue = reference['rpo'] / 2.0 / 3.0 - oci_revenue_mid
    returns = {}
    for tier, name in (('l', 'low'), ('m', 'mid')):
        total = (reference[f'eps_fy28_{name}'] * reference['shares_fy28']
                 - reference['sbc_fy28'] * keep + reference['int_fy28'] * keep)
        returns[tier] = (total - software_nopat) / spent
    return {'roe_l': returns['l'], 'roe_m': returns['m'],
            'capital_book': incremental_revenue / (oci_revenue_mid / spent),
            'capital_aws': incremental_revenue / historical_aws_turnover,
            'incremental_revenue': incremental_revenue}


def historical_meta_invested_capital(reference: dict) -> float:
    """Reconstruct META's frozen 2026-10-29 unrounded capital path.

    The historical formula is separate from any later stock model; deliberately
    changing it or the historical input snapshot requires re-review.
    """
    fields = ('equity', 'net_debt', 'eps_fy26', 'eps_9m_26', 'shares',
              'ocf_ni', 'capex_q4_26', 'eps_m', 'capex_fy27')
    if not isinstance(reference, dict) or any(key not in reference for key in fields):
        raise ValueError('historical capital path is missing fields')
    if any(isinstance(reference[key], bool) or not isinstance(reference[key], (int, float))
           or not isfinite(reference[key]) for key in fields):
        raise ValueError('historical capital path inputs must be finite')
    current = reference['equity'] + reference['net_debt']
    q4_earnings = (reference['eps_fy26'] - reference['eps_9m_26']) * reference['shares']
    fy27_earnings = reference['eps_m'] * reference['shares']
    return (current + q4_earnings + reference['capex_q4_26'] - q4_earnings * reference['ocf_ni']
            + fy27_earnings + reference['capex_fy27'] - fy27_earnings * reference['ocf_ni'])


def replace_capex_reference(adopted_capital: float, current_capex: float, reference_capex: float) -> float:
    """Replace the current one-period capex with an independent reference."""
    values = (adopted_capital, current_capex, reference_capex)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in values):
        raise ValueError('capital and capex references must be finite numbers')
    return adopted_capital + (reference_capex - current_capex)
