"""Value a temporary scenario followed by a separately valued steady scenario."""


def temporary_then_steady(high_income, steady_income, distribution, growth, discount, years, steady_multiple):
    """All amounts start in year one; return their value at the start of year one."""
    if isinstance(years, bool) or not isinstance(years, int) or years < 1:
        raise ValueError("scenario duration must be a positive whole number of years")
    # Each high-year distribution grows from year one; the steady-world income
    # has the same growth adjustment at the transition, as in the original
    # one-year model. Only the caller-selected business receives this treatment.
    annual = sum(high_income * (1 + growth) ** year * distribution / (1 + discount) ** (year + 1)
                 for year in range(years))
    terminal = steady_income * (1 + growth) ** years * steady_multiple / (1 + discount) ** years
    return annual + terminal
