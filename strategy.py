"""Paper-trade engine — two books in parallel + laddered entries.

Goal: measure the REAL option spread AND test whether being a MAKER (post at mid and
wait) beats being a TAKER (cross the spread). Both books run the same laddered short-vol
BTC+ETH + 1d-cashout strategy on R$100k at the chosen leverage; the ONLY difference is
how entries fill:

  TAKER  — fills immediately at the bid (sells vol cheap; pays the half-spread).
  MAKER  — posts a sell at the mid and waits. Fills when the market trades THROUGH the
           level (mark_iv rises to the post — measured, this is the adverse-selection
           channel) OR via uninformed flow at an assumed daily probability. If neither
           within the fill window, the order EXPIRES (missed entry — flat that tranche).

Exits are taker for both books (a spike-cashout must take liquidity; expiry settles
free) so the comparison isolates the entry-execution edge. Laddering: each sleeve opens
a 1/K tranche every ~STAGGER days, so the book is continuous instead of one synchronized
monthly bet. Fees are fixed; the spread is measured live; the uninformed maker-fill
probability is the one labeled assumption.
"""
from __future__ import annotations
import copy
import json
import time
import urllib.request

import numpy as np

import deribit
import store

BANKROLL0 = 100_000.0
F = 0.43                  # chosen ticket: ~25% DD budget (gross)
WEIGHT = {"BTC": 0.5, "ETH": 0.5}
TAU = 30
K_TRANCHES = 4            # ladder: 4 overlapping tranches per sleeve
STAGGER_DAYS = TAU / K_TRANCHES   # ~7.5d between tranche entries
FILL_WINDOW_DAYS = 4      # maker order rests this long before expiring
CASHOUT_K = 1.25
FEE_ROR_RT = 0.008
FEE_ROR_ONE = 0.004
YEAR_S = 365 * 24 * 3600
ASSETS = ["BTC", "ETH"]
BOOKS = ["taker", "maker"]

STATE_VERSION = 3  # bump when the state schema changes -> stale state resets cleanly

DEFAULT_STATE = {
    "_v": STATE_VERSION,
    "books": {b: {"equity": BANKROLL0, "realized": 0.0} for b in BOOKS},
    "tranches": {b: {a: [] for a in ASSETS} for b in BOOKS},
    "pending_maker": {a: [] for a in ASSETS},
    "last_px": {}, "last_tranche_day": {}, "last_straddle": {}, "start_ts": None,
    "maker_stats": {"posted": 0, "filled": 0, "expired": 0},
    "last_decision_day": None,
}


def _load():
    """Load state, resetting to a fresh copy if the persisted schema is stale."""
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


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


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
            # accrue realized variance on every open tranche of this asset (both books)
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
    store.log_equity(st["books"]["taker"]["equity"], st["books"]["maker"]["equity"])
    return snap


def _ann_var(tr):
    el = max(time.time() - tr["entry_ts"], 1.0)
    return tr["sum_r2"] * (YEAR_S / el) if tr["sum_r2"] > 0 else 0.0


def _mark_ror(tr, dvol_now):
    K = tr["strike_iv"] ** 2
    t = min(_days(tr["entry_ts"]), TAU)
    rv2 = _ann_var(tr)
    rem = (dvol_now ** 2) if dvol_now else K
    return (K - ((t / TAU) * rv2 + ((TAU - t) / TAU) * rem)) / K, np.sqrt(rv2)


def _open_tranche(book, cur, strike_iv, equity):
    return {"entry_ts": time.time(), "strike_iv": strike_iv,
            "notional": WEIGHT[cur] * F * equity / K_TRANCHES, "sum_r2": 0.0}


def _close(st, book, cur, tr, tk, cash):
    ror, rv = _mark_ror(tr, tk["dvol"])
    hs = tk["half_spread_vp"] or 0.0
    cost = (2 * hs / max(tr["strike_iv"], 1e-6) + FEE_ROR_RT) if cash else FEE_ROR_ONE
    net = ror - cost
    pnl = tr["notional"] * net
    st["books"][book]["equity"] += pnl
    st["books"][book]["realized"] += pnl
    store.add_trade(cur, ("CASHOUT" if cash else "SETTLE") + "-" + book, days_held=round(_days(tr["entry_ts"]), 1),
                    strike_iv=tr["strike_iv"], rv_ann=rv, ror=ror, cost_ror=cost, pnl_r=pnl)


def decide():
    st = _load()
    today = _today()
    if st.get("last_decision_day") == today:
        return st
    if st["start_ts"] is None:
        st["start_ts"] = time.time()

    for cur in ASSETS:
        rows = store.query("SELECT * FROM ticks WHERE asset=? ORDER BY ts DESC LIMIT 1", (cur,))
        if not rows:
            continue
        tk = rows[0]
        fc = _binance_daily_rv("BTCUSDT" if cur == "BTC" else "ETHUSDT")

        # ---- resolve pending maker orders via the REAL trade tape (no aliasing, no
        #      assumed fill prob): fill if a real BUY trade printed through your level ----
        still = []
        for od in st["pending_maker"][cur]:
            tape = deribit.recent_trades(od["call_instrument"], od["post_ts_ms"], time.time() * 1000)
            through = [t for t in tape if t["direction"] == "buy" and t["iv"] and t["iv"] >= od["call_post_iv_pct"]]
            if through:
                vol_through = round(sum(t["amount"] for t in through), 1)
                st["tranches"]["maker"][cur].append(
                    {"entry_ts": through[0]["ts"] / 1000.0, "strike_iv": od["post_iv"],
                     "notional": od["notional"], "sum_r2": 0.0})
                st["maker_stats"]["filled"] += 1
                store.add_trade(cur, "FILL-maker", strike_iv=od["post_iv"],
                                note=f"real buy through level, {vol_through} contracts")
            elif _days(od["post_ts"]) > FILL_WINDOW_DAYS:
                st["maker_stats"]["expired"] += 1
                store.add_trade(cur, "EXPIRE-maker", strike_iv=od["post_iv"], note="no buyer at level")
            else:
                still.append(od)
        st["pending_maker"][cur] = still

        # ---- ladder: open a new tranche every STAGGER days ----
        lt = st["last_tranche_day"].get(cur)
        ls = st.get("last_straddle", {}).get(cur)
        if (lt is None or _days(lt) >= STAGGER_DAYS) and ls:
            st["tranches"]["taker"][cur].append(_open_tranche("taker", cur, tk["bid_iv"], st["books"]["taker"]["equity"]))
            store.add_trade(cur, "ENTER-taker", strike_iv=tk["bid_iv"], note=f"hs={tk['half_spread_vp']:.4f}")
            st["pending_maker"][cur].append({"post_ts": time.time(), "post_ts_ms": time.time() * 1000,
                                             "post_iv": tk["mark_iv"], "call_instrument": ls["call_instrument"],
                                             "call_post_iv_pct": ls["call_mark_iv_pct"],
                                             "notional": WEIGHT[cur] * F * st["books"]["maker"]["equity"] / K_TRANCHES})
            st["maker_stats"]["posted"] += 1
            store.add_trade(cur, "POST-maker", strike_iv=tk["mark_iv"], note=f"resting at mid on {ls['call_instrument']}")
            st["last_tranche_day"][cur] = time.time()

        # ---- cashout / expiry on every open tranche (both books) ----
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

    st["last_decision_day"] = today
    store.set_state(st)
    return st


def snapshot():
    st = _load()
    marks = {b: [] for b in BOOKS}
    for b in BOOKS:
        for cur in ASSETS:
            t = store.query("SELECT dvol FROM ticks WHERE asset=? ORDER BY ts DESC LIMIT 1", (cur,))
            dv = t[0]["dvol"] if t else None
            for tr in st["tranches"][b][cur]:
                ror, _ = _mark_ror(tr, dv)
                marks[b].append({"asset": cur, "days": round(_days(tr["entry_ts"]), 1),
                                 "strike_iv": tr["strike_iv"], "mark_pnl_r": tr["notional"] * ror})
    return st, marks
