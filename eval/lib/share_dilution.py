"""Calculate dilution from a hypothetical conditional share award."""
from math import isfinite


def conditional_award_dilution(existing_shares: float, award_shares: float) -> float:
    """Return new award holders' share after hypothetical full issuance.

    Counts must use the same unit. The function does not decide whether the
    award will vest or change the actual base share count.
    """
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v)
           for v in (existing_shares, award_shares)):
        raise ValueError('share counts must be finite numbers')
    if existing_shares <= 0 or award_shares < 0:
        raise ValueError('existing shares must be positive and award shares nonnegative')
    return award_shares / (existing_shares + award_shares)
