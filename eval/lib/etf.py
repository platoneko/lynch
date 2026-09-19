"""Total-return trend channel for passive baskets (valuation-rules.md「§ETF 例外」).

A passive basket has no fair value apart from its NAV (price ≡ NAV; the fund
retains nothing, allocates nothing).  Index PE / PB / ROE are snapshots of the
current basket (they jump on rebalance and report dates); only flows — total
return, dividends per point — are continuous.  So the ruler is the total-return
trend channel: M = log-linear trend (compound 中枢, slope = realised central
return), L / H = residual p10 / p90.  Slope below the hurdle means "do not
hold" whatever the position.  E-side quantities (PE percentile, D trend, payout
headroom, basket PB-ROE implied r) are guardrails, printed not priced.
Concentrated baskets (520900: 44% energy in four SOEs, no TR index) are looked
through to the constituent frameworks instead; the price channel is their
cross-check.

Each ETF eval declares its own index (`src`, `code`), regime break and
dividend flag; this module only holds the fetch + cache and the maths.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE = REPO_ROOT / "_local" / "tmp" / "etf_index_hist.json"
RATES_CACHE = REPO_ROOT / "_local" / "tmp" / "cny10y_daily.json"
# r_f 中枢 by currency (valuation-rules「§利率轴」方案 A); replaced by `_AXIS.rates.md` once it lands.
RF_MID = {"CNY": 0.025}
_EM_10Y = "EMM00166466"  # Eastmoney treasury-curve field: CNY 10Y
_CSI_HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.csindex.com.cn/"}

SUBWINDOWS = ("2019-01-01", "2021-01-01")
# Regime break rule: a sub-window slope ≥ 5pp away from the full-window slope
# with a nameable cause → fit the channel from the break; the full window is
# printed as the cross-check.  Declared per ETF as `regime=(start, why)`.
REGIME_GAP = 0.05


# ---- Step 1: index history (cached; csindex / cnindex public APIs) ----
def _fetch(src: str, code: str) -> pd.Series:
    if src == "csi":
        u = f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}&startDate=20151201&endDate=20991231"
        d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=_CSI_HDR), timeout=30))["data"]
        s = pd.Series({pd.Timestamp(x["tradeDate"]): x["close"] for x in d})
    else:
        u = (f"https://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat?indexCode={code}"
             f"&startDate=2015-12-01&endDate=2099-12-31&frequency=day")
        d = json.load(urllib.request.urlopen(u, timeout=30))["data"]
        df = pd.DataFrame(d["data"], columns=d["item"])
        s = pd.Series(df["close"].astype(float).values, index=pd.to_datetime(df["timestamp"]))
    return s.dropna().sort_index()


def history(src: str, code: str) -> pd.Series:
    """Daily index closes (date index), fetched once and cached under _local/tmp."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    if code not in cache:
        s = _fetch(src, code)
        cache[code] = {d.strftime("%Y-%m-%d"): float(v) for d, v in s.items()}
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
    s = pd.Series(cache[code])
    s.index = pd.to_datetime(s.index)
    return s[s > 0].sort_index()


# ---- Step 2: trend channel ----
def channel(s: pd.Series) -> dict:
    """Log-linear fit, outliers (>2σ) dropped and refit; M = trend, L/H = residual p10/p90.

    Keys: slope (annual, decimal), sigma, z (latest residual / σ), L / M / H in
    index points at the last date, cur (last close), start / end (ISO dates).
    """
    t = np.array((s.index - s.index[0]).days / 365.25)
    y = np.log(s.values)
    b, a = np.polyfit(t, y, 1)
    keep = np.abs(y - (a + b * t)) < 2 * (y - (a + b * t)).std()
    b, a = np.polyfit(t[keep], y[keep], 1)
    res = y - (a + b * t)
    lo, hi = np.quantile(res, [0.10, 0.90])
    M = float(np.exp(a + b * t[-1]))
    return dict(slope=float(np.exp(b) - 1), sigma=float(res.std()), z=float(res[-1] / res.std()),
                L=M * float(np.exp(lo)), M=M, H=M * float(np.exp(hi)), cur=float(s.values[-1]),
                start=s.index[0].date().isoformat(), end=s.index[-1].date().isoformat())


def slope_since(s: pd.Series, start: str) -> float:
    ss = s[s.index >= start]
    t = np.array((ss.index - ss.index[0]).days / 365.25)
    b, _ = np.polyfit(t, np.log(ss.values), 1)
    return float(np.exp(b) - 1)


# ---- Step 2b: rate-neutral cross-read (dividend ETFs) ----
def rates(ccy: str) -> pd.Series:
    """Listing-currency 10Y daily yield (decimal). CNY: Eastmoney treasury curve, paged 500/req, cached."""
    assert ccy == "CNY", f"rates(): only CNY wired; {ccy} pending _AXIS.rates.md"
    if not RATES_CACHE.exists():
        out = {}
        for p in range(1, 60):
            u = ("https://datacenter-web.eastmoney.com/api/data/get?type=RPTA_WEB_TREASURYYIELD"
                 f"&sty=ALL&st=SOLAR_DATE&sr=-1&p={p}&ps=500")
            rows = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=_CSI_HDR), timeout=60))["result"]["data"]
            out.update({r["SOLAR_DATE"][:10]: r[_EM_10Y] for r in rows if r[_EM_10Y] is not None})
            if rows[-1]["SOLAR_DATE"][:10] < "2015-11-01" or len(rows) < 500:
                break
        RATES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        RATES_CACHE.write_text(json.dumps(out))
    r = pd.Series(json.loads(RATES_CACHE.read_text())) / 100
    r.index = pd.to_datetime(r.index)
    return r.sort_index()


def rate_neutral(s: pd.Series, rf: pd.Series, rf_mid: float, dy: float) -> dict:
    """Joint fit ln S = a + b t + β (r_f − r_f 中枢), outliers dropped and refit.

    b is the slope with the rate tailwind removed; M′ is the trend at r_f = 中枢; L′/H′ = ε p10/p90
    where ε is what rates do not explain.  β robustness is printed three ways (level, monthly
    first-difference, prior −1/DY) because the level β is collinear with t; the zone′ uses the
    level β, the prior is only a pure-perpetuity stress row.
    """
    df = pd.DataFrame({"s": s}).join(rf.rename("rf"), how="left").ffill().dropna()
    t = np.array((df.index - df.index[0]).days / 365.25)
    y, r = np.log(df.s.values), df.rf.values
    X = np.c_[np.ones_like(t), t, r - rf_mid]
    c = np.linalg.lstsq(X, y, rcond=None)[0]
    keep = np.abs(y - X @ c) < 2 * (y - X @ c).std()
    c = np.linalg.lstsq(X[keep], y[keep], rcond=None)[0]
    a, b, beta = c
    res = y - X @ c
    lo, hi = np.quantile(res, [0.10, 0.90])
    M = float(np.exp(a + b * t[-1]))
    # robustness: monthly first differences (not collinear with t)
    m = df.resample("ME").last()
    dy_, dr = np.diff(np.log(m.s.values)), np.diff(m.rf.values)
    beta_d, _ = np.polyfit(dr, dy_, 1)
    # stress: impose the pure-perpetuity prior β = −1/DY, refit slope only
    beta_p = -1 / dy
    bp, ap = np.polyfit(t, y - beta_p * (r - rf_mid), 1)
    Mp = float(np.exp(ap + bp * t[-1]))
    return dict(slope=float(np.exp(b) - 1), beta=float(beta), beta_diff=float(beta_d),
                rho_diff=float(np.corrcoef(dr, dy_)[0, 1]), beta_prior=beta_p,
                rate_drift=float(beta * (r[-1] - r[0]) / t[-1]),  # slope pp/yr attributable to the r_f path
                sigma=float(res.std()), z=float(res[-1] / res.std()),
                L=M * float(np.exp(lo)), M=M, H=M * float(np.exp(hi)), cur=float(df.s.values[-1]),
                rf_now=float(r[-1]), rf_start=float(r[0]), rf_mid=rf_mid,
                prior=dict(slope=float(np.exp(bp) - 1), M=Mp, vsM=float(df.s.values[-1] / Mp - 1)))
