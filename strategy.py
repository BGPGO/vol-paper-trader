"""Paper-trade engine — TWO independent universes, each with its own dashboard tab:

  core : BTC, ETH       (liquid; have a clean DVOL implied-vol index + full backtest)
  alts : SOL, XRP, HYPE, AVAX  (USDC-linear options; no DVOL/no history -> we collect
                                 IV/spread/RV FORWARD here so a real backtest becomes
                                 possible later)

Within each universe, three execution books run on their OWN R$100k (not split): TAKER
(cross at bid), MAKER (post mid, fills only on a real buy-through the tape, can miss),
CHASE (post mid -> cross to bid at the deadline). Strategy = laddered short-vol +
1-day CatBoost cashout. Fees fixed; spread measured live; fills from the real trade tape.
"""
from __future__ import annotations
import copy
import time

import numpy as np

import deribit
import forecast_1d
import store

BANKROLL0 = 100_000.0      # PER BOOK PER UNIVERSE (independent)
F = 0.43
TAU = 30
K_TRANCHES = 4
STAGGER_DAYS = TAU / K_TRANCHES
FILL_WINDOW_DAYS = 4
CHASE_DEADLINE_DAYS = 2
CASHOUT_K = 1.25
FEE_ROR_RT = 0.008
FEE_ROR_ONE = 0.004
YEAR_S = 365 * 24 * 3600
BOOKS = ["taker", "maker", "chase"]
MAKER_BOOKS = ["maker", "chase"]

UNIVERSES = {"core": ["BTC", "ETH"], "alts": ["SOL", "XRP", "HYPE", "AVAX"]}
ASSET_CFG = {
    "BTC": ("BTC", "BTCUSDT", True), "ETH": ("ETH", "ETHUSDT", True),
    "SOL": ("USDC", "SOLUSDT", False), "XRP": ("USDC", "XRPUSDT", False),
    "HYPE": ("USDC", "HYPEUSDT", False), "AVAX": ("USDC", "AVAXUSDT", False),
}  # asset -> (option currency, binance symbol, has_DVOL)
ALL_ASSETS = [a for u in UNIVERSES.values() for a in u]
ASSET_UNI = {a: u for u, assets in UNIVERSES.items() for a in assets}
WEIGHT = {a: 1.0 / len(UNIVERSES[ASSET_UNI[a]]) for a in ALL_ASSETS}

STATE_VERSION = 5


def _new_uni():
    return {"books": {b: {"equity": BANKROLL0, "realized": 0.0} for b in BOOKS},
            "tranches": {b: {a: [] for a in ALL_ASSETS} for b in BOOKS},
            "pending": {b: {a: [] for a in ALL_ASSETS} for b in MAKER_BOOKS},
            "stats": {"maker": {"posted": 0, "filled": 0, "expired": 0},
                      "chase": {"posted": 0, "filled": 0, "crossed": 0}},
            "last_tranche_day": {}}


def _default():
    return {"_v": STATE_VERSION, "universes": {u: _new_uni() for u in UNIVERSES},
            "last_px": {}, "last_straddle": {}, "start_ts": None}


def _load():
    st = store.get_state(None)
    if not st or st.get("_v") != STATE_VERSION:
        return _default()
    return st


def _days(ts):
    return (time.time() - ts) / 86400.0


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def poll_and_log():
    st = _load()
    snap = {}
    for cur in ALL_ASSETS:
        opt_ccy, _, has_dvol = ASSET_CFG[cur]
        try:
            s = deribit.atm_straddle_iv(cur, opt_ccy)
            if not s or not s.get("mark_iv"):
                snap[cur] = {"error": "no chain"}; continue
            dv = deribit.dvol(cur) if has_dvol else s["mark_iv"]   # alts: chain ATM as implied
            fund = deribit.perp_funding(cur) if has_dvol else None
            d = {"index_px": s["underlying_price"], "dvol": dv, "mark_iv": s["mark_iv"],
                 "bid_iv": s["bid_iv"], "ask_iv": s["ask_iv"], "half_spread_vp": s["half_spread_vp"],
                 "funding8h": fund}
            store.add_tick(cur, d); snap[cur] = d
            uni = st["universes"][ASSET_UNI[cur]]
            last = st["last_px"].get(cur)
            if last and d["index_px"]:
                r2 = np.log(d["index_px"] / last) ** 2
                for b in BOOKS:
                    for tr in uni["tranches"][b][cur]:
                        tr["sum_r2"] += r2
            if d["index_px"]:
                st["last_px"][cur] = d["index_px"]
            st.setdefault("last_straddle", {})[cur] = {
                "call_instrument": s["call_instrument"], "call_mark_iv_pct": s["call_mark_iv_pct"]}
        except Exception as e:  # noqa: BLE001
            snap[cur] = {"error": str(e)}
    store.set_state(st)
    dvols = {c: snap[c]["mark_iv"] for c in ALL_ASSETS if c in snap and "mark_iv" in snap[c]}
    eq = {}
    for u in UNIVERSES:
        for b, v in marked_equity(st["universes"][u], dvols).items():
            eq[f"{u}:{b}"] = v
    store.log_equity(eq)
    return snap


def marked_equity(uni, dvols):
    out = {}
    for b in BOOKS:
        e = uni["books"][b]["equity"]
        for cur in ALL_ASSETS:
            for tr in uni["tranches"][b][cur]:
                ror, _ = _mark_ror(tr, dvols.get(cur))
                e += tr["notional"] * ror
        out[b] = e
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


def _close(uni, book, cur, tr, tk, cash):
    ror, rv = _mark_ror(tr, tk["mark_iv"])  # mark ATM-to-ATM, not DVOL
    hs = tk["half_spread_vp"] or 0.0
    cost = (2 * hs / max(tr["strike_iv"], 1e-6) + FEE_ROR_RT) if cash else FEE_ROR_ONE
    pnl = tr["notional"] * (ror - cost)
    uni["books"][book]["equity"] += pnl
    uni["books"][book]["realized"] += pnl
    store.add_trade(cur, ("CASHOUT" if cash else "SETTLE") + "-" + book,
                    days_held=round(_days(tr["entry_ts"]), 1), strike_iv=tr["strike_iv"],
                    rv_ann=rv, ror=ror, cost_ror=cost, pnl_r=pnl)


def decide():
    st = _load()
    if st["start_ts"] is None:
        st["start_ts"] = time.time()
    now_ms = time.time() * 1000
    for cur in ALL_ASSETS:
        rows = store.query("SELECT * FROM ticks WHERE asset=? ORDER BY ts DESC LIMIT 1", (cur,))
        if not rows:
            continue
        tk = rows[0]
        uni = st["universes"][ASSET_UNI[cur]]
        binance = ASSET_CFG[cur][1]
        fc = forecast_1d.predict_next_day_vol(binance, _today())

        for b in MAKER_BOOKS:
            still = []
            for od in uni["pending"][b][cur]:
                tape = deribit.recent_trades(od["call_instrument"], od["post_ts_ms"], now_ms)
                through = [t for t in tape if t["direction"] == "buy" and t["iv"] and t["iv"] >= od["call_post_iv_pct"]]
                if through:
                    vt = round(sum(t["amount"] for t in through), 1)
                    uni["tranches"][b][cur].append(_tranche(od["post_iv"], od["notional"], through[0]["ts"] / 1000.0))
                    uni["stats"][b]["filled"] += 1
                    store.add_trade(cur, "FILL-" + b, strike_iv=od["post_iv"], note=f"buy through, {vt} contr")
                elif b == "chase" and _days(od["post_ts"]) >= CHASE_DEADLINE_DAYS:
                    uni["tranches"]["chase"][cur].append(_tranche(tk["bid_iv"], od["notional"]))
                    uni["stats"]["chase"]["crossed"] += 1
                    store.add_trade(cur, "CROSS-chase", strike_iv=tk["bid_iv"], note="deadline -> bid")
                elif b == "maker" and _days(od["post_ts"]) > FILL_WINDOW_DAYS:
                    uni["stats"]["maker"]["expired"] += 1
                    store.add_trade(cur, "EXPIRE-maker", strike_iv=od["post_iv"], note="no buyer")
                else:
                    still.append(od)
            uni["pending"][b][cur] = still

        lt = uni["last_tranche_day"].get(cur)
        ls = st.get("last_straddle", {}).get(cur)
        if (lt is None or _days(lt) >= STAGGER_DAYS) and ls and tk["bid_iv"]:
            uni["tranches"]["taker"][cur].append(
                _tranche(tk["bid_iv"], WEIGHT[cur] * F * uni["books"]["taker"]["equity"] / K_TRANCHES))
            store.add_trade(cur, "ENTER-taker", strike_iv=tk["bid_iv"], note=f"hs={tk['half_spread_vp']:.4f}")
            for b in MAKER_BOOKS:
                uni["pending"][b][cur].append(
                    {"post_ts": time.time(), "post_ts_ms": now_ms, "post_iv": tk["mark_iv"],
                     "call_instrument": ls["call_instrument"], "call_post_iv_pct": ls["call_mark_iv_pct"],
                     "notional": WEIGHT[cur] * F * uni["books"][b]["equity"] / K_TRANCHES})
                uni["stats"][b]["posted"] += 1
                store.add_trade(cur, "POST-" + b, strike_iv=tk["mark_iv"], note="resting at mid")
            uni["last_tranche_day"][cur] = time.time()

        for b in BOOKS:
            keep = []
            for tr in uni["tranches"][b][cur]:
                days = _days(tr["entry_ts"])
                spike = fc is not None and fc > CASHOUT_K * tr["strike_iv"]
                if spike or days >= TAU:
                    _close(uni, b, cur, tr, tk, cash=spike and days < TAU)
                else:
                    keep.append(tr)
            uni["tranches"][b][cur] = keep
    store.set_state(st)
    return st


def snapshot(universe):
    st = _load()
    uni = st["universes"][universe]
    assets = UNIVERSES[universe]
    dvols = {}
    for cur in assets:
        t = store.query("SELECT mark_iv FROM ticks WHERE asset=? ORDER BY ts DESC LIMIT 1", (cur,))
        dvols[cur] = t[0]["mark_iv"] if t else None
    marks = {b: [] for b in BOOKS}
    for b in BOOKS:
        for cur in assets:
            for tr in uni["tranches"][b][cur]:
                ror, _ = _mark_ror(tr, dvols.get(cur))
                marks[b].append({"asset": cur, "days": round(_days(tr["entry_ts"]), 1),
                                 "strike_iv": tr["strike_iv"], "mark_pnl_r": tr["notional"] * ror})
    return uni, marks, marked_equity(uni, dvols)
