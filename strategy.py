"""Paper-trade engine: short-vol BTC+ETH + 1d cashout, sized on R$100k at the chosen
leverage. Primary purpose is to log the REAL option spread; the paper P&L uses the same
variance-swap convention as the research backtest but charges the *measured* live spread
instead of an assumed one. Fees are fixed constants; the spread is what we monitor.
"""
from __future__ import annotations
import json
import time
import urllib.request

import numpy as np

import deribit
import store

BANKROLL0 = 100_000.0
F = 0.43                 # chosen ticket: levered to ~25% DD (gross) per the backtest
WEIGHT = {"BTC": 0.5, "ETH": 0.5}
TAU = 30                 # 30-day cycle
CASHOUT_K = 1.25         # cash out if forecast next-day vol > K * strike
FEE_ROR_RT = 0.008       # fixed Deribit fees, round-trip (cashout)
FEE_ROR_ONE = 0.004      # fixed fees, hold-to-expiry (entry + settle)
YEAR_S = 365 * 24 * 3600
ASSETS = ["BTC", "ETH"]

DEFAULT_STATE = {"equity": BANKROLL0, "realized_pnl": 0.0, "start_ts": None,
                 "pos": {a: None for a in ASSETS}, "last_decision_day": None}


def _binance_daily_rv(symbol, days=40):
    """Trailing daily realized vol (annualized) from Binance daily klines — for the
    lightweight cashout signal (a proxy for the research 1d forecaster)."""
    url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit={days+1}")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            kl = json.load(r)
        closes = np.array([float(k[4]) for k in kl])
        ret = np.diff(np.log(closes))
        # HAR-lite next-day vol: blend of 1d, 5d, 22d trailing vol (annualized)
        def vol(n):
            return np.std(ret[-n:], ddof=0) * np.sqrt(365) if len(ret) >= n else np.nan
        return float(np.nanmean([vol(2), vol(5), vol(22)]))
    except Exception:
        return None


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _days_between(d0):
    return (time.time() - d0) / 86400.0


def poll_and_log():
    """Fetch live market data for both assets, log a tick (spread monitor), update marks."""
    snap = {}
    for cur in ASSETS:
        try:
            s = deribit.atm_straddle_iv(cur)
            d = {"index_px": deribit.index_price(cur), "dvol": deribit.dvol(cur),
                 "mark_iv": s["mark_iv"], "bid_iv": s["bid_iv"], "ask_iv": s["ask_iv"],
                 "half_spread_vp": s["half_spread_vp"], "funding8h": deribit.perp_funding(cur)}
            store.add_tick(cur, d)
            snap[cur] = d
        except Exception as e:  # noqa: BLE001
            snap[cur] = {"error": str(e)}
    _accrue_realized(snap)
    return snap


def _accrue_realized(snap):
    st = store.get_state(DEFAULT_STATE)
    for cur in ASSETS:
        pos = st["pos"].get(cur)
        if not pos or cur not in snap or "index_px" not in snap[cur]:
            continue
        px = snap[cur]["index_px"]
        last = pos.get("last_px")
        if last:
            r = np.log(px / last)
            pos["sum_r2"] = pos.get("sum_r2", 0.0) + r * r
            pos["n_obs"] = pos.get("n_obs", 0) + 1
        pos["last_px"] = px
        pos["last_dvol"] = snap[cur]["dvol"]
    store.set_state(st)


def _realized_ann_var(pos):
    elapsed = max(time.time() - pos["entry_ts"], 1.0)
    if pos.get("sum_r2", 0) <= 0:
        return 0.0
    return pos["sum_r2"] * (YEAR_S / elapsed)


def mark_pnl_ror(pos, dvol_now):
    """Short-variance mark in return-on-strike units (same convention as the backtest)."""
    K = pos["strike_iv"] ** 2
    t = min(_days_between(pos["entry_ts"]), TAU)
    rv2 = _realized_ann_var(pos)
    remaining = (dvol_now ** 2) if dvol_now else K
    mark = K - ((t / TAU) * rv2 + ((TAU - t) / TAU) * remaining)
    return mark / K, np.sqrt(rv2)


def decide():
    """Once per day: open new straddles on the 30d schedule, cash out on the 1d signal."""
    st = store.get_state(DEFAULT_STATE)
    today = _today()
    if st.get("last_decision_day") == today:
        return st
    if st["start_ts"] is None:
        st["start_ts"] = time.time()

    for cur in ASSETS:
        ticks = store.query("SELECT * FROM ticks WHERE asset=? ORDER BY ts DESC LIMIT 1", (cur,))
        if not ticks:
            continue
        tk = ticks[0]
        pos = st["pos"].get(cur)
        notional = WEIGHT[cur] * F * st["equity"]

        if pos is None:
            # entry gate: first day, or >= TAU days since last entry for this sleeve
            last_entry = st.get("last_entry_ts", {}).get(cur) if isinstance(st.get("last_entry_ts"), dict) else None
            if last_entry and _days_between(last_entry) < TAU:
                continue
            st["pos"][cur] = {"entry_ts": time.time(), "strike_iv": tk["bid_iv"],  # sold at bid
                              "notional": notional, "sum_r2": 0.0, "n_obs": 0,
                              "last_px": tk["index_px"], "last_dvol": tk["dvol"]}
            st.setdefault("last_entry_ts", {})[cur] = time.time()
            store.add_trade(cur, "ENTER", strike_iv=tk["bid_iv"], note=f"half_spread={tk['half_spread_vp']}")
            continue

        # in position: cashout signal or expiry
        days = _days_between(pos["entry_ts"])
        fc = _binance_daily_rv("BTCUSDT" if cur == "BTC" else "ETHUSDT")
        ror, rv = mark_pnl_ror(pos, tk["dvol"])
        spike = fc is not None and fc > CASHOUT_K * pos["strike_iv"]
        if spike or days >= TAU:
            cash = spike and days < TAU
            # exit cost: buy back at ask (cashout) -> 2 half-spreads; expiry -> entry spread only
            hs = tk["half_spread_vp"] or 0.0
            spread_ror = 2.0 * hs / max(pos["strike_iv"], 1e-6) * (2.0 if cash else 1.0)
            fee = FEE_ROR_RT if cash else FEE_ROR_ONE
            cost = spread_ror + fee
            net_ror = ror - cost
            pnl = pos["notional"] * net_ror
            st["equity"] += pnl
            st["realized_pnl"] += pnl
            store.add_trade(cur, "CASHOUT" if cash else "SETTLE", days_held=round(days, 1),
                            strike_iv=pos["strike_iv"], rv_ann=rv, ror=ror, cost_ror=cost,
                            pnl_r=pnl, note=f"fc={fc}")
            st["pos"][cur] = None
    st["last_decision_day"] = today
    store.set_state(st)
    return st


def snapshot():
    st = store.get_state(DEFAULT_STATE)
    open_marks = {}
    for cur in ASSETS:
        pos = st["pos"].get(cur)
        if pos:
            t = store.query("SELECT dvol FROM ticks WHERE asset=? ORDER BY ts DESC LIMIT 1", (cur,))
            dv = t[0]["dvol"] if t else None
            ror, rv = mark_pnl_ror(pos, dv)
            open_marks[cur] = {"days": round(_days_between(pos["entry_ts"]), 1),
                               "strike_iv": pos["strike_iv"], "mark_ror": ror,
                               "mark_pnl_r": pos["notional"] * ror}
    return st, open_marks
