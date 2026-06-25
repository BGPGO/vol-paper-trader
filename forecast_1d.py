"""CatBoost next-day vol forecaster for the live cashout signal.

The backtest horse-race showed CatBoost beats HAR for the 1-DAY horizon (data-rich +
microstructure helps), so the paper trader's cashout uses it. Trained on ~2y of Binance
daily klines, features = HAR lags (Corsi d/w/m) + momentum + range + dollar-vol, target =
next-day Parkinson log-variance. Retrained once/day (cached); returns next-day annualized
vol. Lighter than the research panel (daily bars, not intraday) but a real CatBoost.
"""
from __future__ import annotations
import json
import urllib.request

import numpy as np

_CACHE = {}  # (symbol, day) -> forecast


def _daily(symbol, days=760):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit={min(days,1000)}"
    with urllib.request.urlopen(url, timeout=25) as r:
        kl = json.load(r)
    o = np.array([float(k[1]) for k in kl]); h = np.array([float(k[2]) for k in kl])
    lo = np.array([float(k[3]) for k in kl]); c = np.array([float(k[4]) for k in kl])
    qv = np.array([float(k[7]) for k in kl])
    return o, h, lo, c, qv


def predict_next_day_vol(symbol, day_key):
    """Annualized next-day vol forecast via CatBoost. Cached per (symbol, day_key)."""
    ck = (symbol, day_key)
    if ck in _CACHE:
        return _CACHE[ck]
    try:
        from catboost import CatBoostRegressor
        o, h, lo, c, qv = _daily(symbol)
        logret = np.diff(np.log(c), prepend=np.log(c[0]))
        pk_var = (1.0 / (4 * np.log(2))) * np.log(h / lo) ** 2 * 365.0      # daily annualized var (Parkinson)
        lrv = np.log(np.clip(pk_var, 1e-8, None))
        n = len(c)
        def roll(a, w):
            return np.array([a[max(0, i - w + 1):i + 1].mean() for i in range(n)])
        feats = np.column_stack([
            lrv, roll(lrv, 5), roll(lrv, 22),                 # HAR d/w/m
            logret, roll(logret, 5), roll(logret, 22),        # momentum
            np.log(h / lo), roll(np.log(h / lo), 5),          # range
            np.log1p(qv) - roll(np.log1p(qv), 22),            # dollar-vol z-ish
        ])
        target = np.roll(lrv, -1)                             # next-day log variance
        X, y = feats[30:n - 1], target[30:n - 1]              # warmup + drop last (no target)
        m = CatBoostRegressor(loss_function="RMSE", iterations=400, learning_rate=0.04,
                              depth=4, l2_leaf_reg=6.0, random_seed=42, verbose=0)
        m.fit(X, y)
        pred_logvar = float(m.predict(feats[n - 1:n])[0])     # forecast for tomorrow
        vol = float(np.sqrt(np.exp(np.clip(pred_logvar, -40, 40))))
        _CACHE.clear(); _CACHE[ck] = vol                      # keep cache tiny
        return vol
    except Exception as e:  # noqa: BLE001
        print("forecast_1d error:", e, flush=True)
        return None
