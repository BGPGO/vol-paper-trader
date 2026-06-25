"""Paper-trade engine — THREE independent books, each on its own R$100k bankroll
(NOT split), running the same laddered short-vol BTC+ETH + 1d-cashout strategy. They
differ only in how ENTRIES execute, to compare execution styles head-to-head:

  TAKER  — crosses the spread, fills immediately at the bid (pays the half-spread).
  MAKER  — posts a sell at the mid and waits. Fills only when a real BUY trade prints
           through the level on Deribit's tape (measured, no aliasing). If no buyer in
           FILL_WINDOW days, the order EXPIRES (missed entry — flat that tranche).
  CHASE  — posts at the mid like MAKER, but if unfilled by CHASE_DEADLINE days it CROSSES
           to the bid (taker) — guaranteed fill, capped at the taker cost. Best of both:
           captures the spread when a buyer comes, never gets left out.

Exits are taker for all books (a spike-cashout takes liquidity; expiry settles free).
Ladder: each sleeve opens a 1/K tranche every ~STAGGER days. Fees fixed; spread measured
live; fills from the real trade tape (no assumed fill probability).
"""
from __future__ import annotations
import copy
import json
import time
import urllib.request

import numpy as np

import deribit
import store

BANKROLL0 = 100_000.0      # PER BOOK (each book gets a full R$100k, independent)
F = 0.43
WEIGHT = {"BTC": 0.5, "ETH": 0.5}
TAU = 30
K_TRANCHES = 4
STAGGER_DAYS = TAU / K_TRANCHES
FILL_WINDOW_DAYS = 4       # MAKER expires (misses) after this
CHASE_DEADLINE_DAYS = 2    # CHASE crosses to taker after this
CASHOUT_K = 1.25
FEE_ROR_RT = 0.008
FEE_ROR_ONE = 0.004
YEAR_S = 365 * 24 * 3600
ASSETS = ["BTC", "ETH"]
BOOKS = ["taker", "maker", "chase"]
MAKER_BOOKS = ["maker", "chase"]   # books that post-and-wait

STATE_VERSION = 4

DEFAULT_STATE = {
    "_v": STATE_VERSION,
    "books": {b: {"equity": BANKROLL0, "realized": 0.0} for b in BOOKS},
    "tranches": {b: {a: [] for a in ASSETS} for b in BOOKS},
    "pending": {b: {a: [] for a in ASSETS} for b in MAKER_BOOKS},
    "stats": {"maker": {"posted": 0, "filled": 0, "expired": 0},
              "chase": {"posted": 0, "filled": 0, "crossed": 0}},
    "last_px": {}, "last_tranche_day": {}, "last_straddle": {}, "start_ts": None,
}


def _load():
    st = store.get_state(None)
    if not st or st.get("_v") != STATE_VERSION:
        return copy.deepcopy(DEFAULT_STATE)
    return st


def _binance_daily_rv(symbol, days=40):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit={days+1}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            kl = json.load(r)
        ret = np.diff(np.log(np.array([float(k[4]) for k in kl])))
        def vol(n):
            return np.std(ret[-n:], ddof=0) * np.sqrt(365) if len(ret) >= n else np.nan
        return float(np.nanmean([vol(2), vol(5), vol(22)]))
    except Exception:
        return None


def _days(ts):
    return (time.time() - ts) / 86400.0


def poll_and_log():
    st = _load()
    snap = {}
    for cur in ASSETS:
        try:
            s = deribit.atm_straddle_iv(cur)
            d = {"index_px": deribit.index_price(cur), "dvol": deribit.dvol(cur),
                 "mark_iv": s["mark_iv"], "bid_iv": s["bid_iv"], "ask_iv": s["ask_iv"],
                 "half_spread_vp": s["half_spread_vp"], "funding8h": deribit.perp_funding(cur)}
            store.add_tick(cur, d); snap[cur] = d
            last = st["last_px"].get(cur)
            if last:
                r2 = np.log(d["index_px"] / last) ** 2
                for b in BOOKS:
                    for tr in st["tranches"][b][cur]:
                        tr["sum_r2"] += r2
            st["last_px"][cur] = d["index_px"]
            st.setdefault("last_straddle", {})[cur] = {
                "call_instrument": s["call_instrument"], "call_mark_iv_pct": s["call_mark_iv_pct"]}
        except Exception as e:  # noqa: BLE001
            snap[cur] = {"error": str(e)}
    store.set_state(st)
    dvols = {c: snap[c]["dvol"] for c in ASSETS if c in snap and "dvol" in snap[c]}
    store.log_equity(marked_equity(st, dvols))
    return snap


def marked_equity(st, dvols):
    out = {}
    for b in BOOKS:
        eq = st["books"][b]["equity"]
        for cur in ASSETS:
            for tr in st["tranches"][b][cur]:
                ror, _ = _mark_ror(tr, dvols.get(cur))
                eq += tr["notional"] * ror
        out[b] = eq
    return out


def _ann_var(tr):
    el = max(time.time() - tr["entry_ts"], 1.0)
    return tr["sum_r2"] * (YEAR_S / el) if tr["sum_r2"] > 0 else 0.0


def _mark_ror(tr, dvol_now):
    K = tr["strike_iv"] ** 2
    t = min(_days(tr["entry_ts"]), TAU)
    rv2 = _ann_var(tr)
    rem = (dvol_now ** 2) if dvol_now else K
    return (K - ((t / TAU) * rv2 + ((TAU - t) / TAU) * rem)) / K, np.sqrt(rv2)


def _tranche(strike_iv, notional, entry_ts=None):
    return {"entry_ts": entry_ts or time.time(), "strike_iv": strike_iv,
            "notional": notional, "sum_r2": 0.0}


def _close(st, book, cur, tr, tk, cash):
    ror, rv = _mark_ror(tr, tk["dvol"])
    hs = tk["half_spread_vp"] or 0.0
    cost = (2 * hs / max(tr["strike_iv"], 1e-6) + FEE_ROR_RT) if cash else FEE_ROR_ONE
    pnl = tr["notional"] * (ror - cost)
    st["books"][book]["equity"] += pnl
    st["books"][book]["realized"] += pnl
    store.add_trade(cur, ("CASHOUT" if cash else "SETTLE") + "-" + book,
                    days_held=round(_days(tr["entry_ts"]), 1), strike_iv=tr["strike_iv"],
                    rv_ann=rv, ror=ror, cost_ror=cost, pnl_r=pnl)


def decide():
    st = _load()
    if st["start_ts"] is None:
        st["start_ts"] = time.time()
    now_ms = time.time() * 1000

    for cur in ASSETS:
        rows = store.query("SELECT * FROM ticks WHERE asset=? ORDER BY ts DESC LIMIT 1", (cur,))
        if not rows:
            continue
        tk = rows[0]
        fc = _binance_daily_rv("BTCUSDT" if cur == "BTC" else "ETHUSDT")

        # ---- resolve pending maker/chase orders via the REAL trade tape ----
        for b in MAKER_BOOKS:
            still = []
            for od in st["pending"][b][cur]:
                tape = deribit.recent_trades(od["call_instrument"], od["post_ts_ms"], now_ms)
                through = [t for t in tape if t["direction"] == "buy" and t["iv"] and t["iv"] >= od["call_post_iv_pct"]]
                if through:
                    vol_t = round(sum(t["amount"] for t in through), 1)
                    st["tranches"][b][cur].append(_tranche(od["post_iv"], od["notional"], through[0]["ts"] / 1000.0))
                    st["stats"][b]["filled"] += 1
                    store.add_trade(cur, "FILL-" + b, strike_iv=od["post_iv"], note=f"buy through, {vol_t} contr")
                elif b == "chase" and _days(od["post_ts"]) >= CHASE_DEADLINE_DAYS:
                    st["tranches"]["chase"][cur].append(_tranche(tk["bid_iv"], od["notional"]))  # cross to bid
                    st["stats"]["chase"]["crossed"] += 1
                    store.add_trade(cur, "CROSS-chase", strike_iv=tk["bid_iv"], note="deadline -> taker at bid")
                elif b == "maker" and _days(od["post_ts"]) > FILL_WINDOW_DAYS:
                    st["stats"]["maker"]["expired"] += 1
                    store.add_trade(cur, "EXPIRE-maker", strike_iv=od["post_iv"], note="no buyer at level")
                else:
                    still.append(od)
            st["pending"][b][cur] = still

        # ---- ladder: open a new tranche every STAGGER days (each book on its own bank) ----
        lt = st["last_tranche_day"].get(cur)
        ls = st.get("last_straddle", {}).get(cur)
        if (lt is None or _days(lt) >= STAGGER_DAYS) and ls:
            st["tranches"]["taker"][cur].append(
                _tranche(tk["bid_iv"], WEIGHT[cur] * F * st["books"]["taker"]["equity"] / K_TRANCHES))
            store.add_trade(cur, "ENTER-taker", strike_iv=tk["bid_iv"], note=f"hs={tk['half_spread_vp']:.4f}")
            for b in MAKER_BOOKS:
                st["pending"][b][cur].append(
                    {"post_ts": time.time(), "post_ts_ms": now_ms, "post_iv": tk["mark_iv"],
                     "call_instrument": ls["call_instrument"], "call_post_iv_pct": ls["call_mark_iv_pct"],
                     "notional": WEIGHT[cur] * F * st["books"][b]["equity"] / K_TRANCHES})
                st["stats"][b]["posted"] += 1
                store.add_trade(cur, "POST-" + b, strike_iv=tk["mark_iv"], note="resting at mid")
            st["last_tranche_day"][cur] = time.time()

        # ---- cashout / expiry on every open tranche (all books) ----
        for b in BOOKS:
            keep = []
            for tr in st["tranches"][b][cur]:
                days = _days(tr["entry_ts"])
                spike = fc is not None and fc > CASHOUT_K * tr["strike_iv"]
                if spike or days >= TAU:
                    _close(st, b, cur, tr, tk, cash=spike and days < TAU)
                else:
                    keep.append(tr)
            st["tranches"][b][cur] = keep

    store.set_state(st)
    return st


def snapshot():
    st = _load()
    dvols = {}
    for cur in ASSETS:
        t = store.query("SELECT dvol FROM ticks WHERE asset=? ORDER BY ts DESC LIMIT 1", (cur,))
        dvols[cur] = t[0]["dvol"] if t else None
    marks = {b: [] for b in BOOKS}
    for b in BOOKS:
        for cur in ASSETS:
            for tr in st["tranches"][b][cur]:
                ror, _ = _mark_ror(tr, dvols.get(cur))
                marks[b].append({"asset": cur, "days": round(_days(tr["entry_ts"]), 1),
                                 "strike_iv": tr["strike_iv"], "mark_pnl_r": tr["notional"] * ror})
    return st, marks, marked_equity(st, dvols)
