"""Deribit public-API client for the paper trader. Market data only, no key needed."""
from __future__ import annotations
import json, urllib.request

BASE = "https://www.deribit.com/api/v2/public/"


def _get(method, **p):
    q = "&".join(f"{k}={v}" for k, v in p.items())
    with urllib.request.urlopen(f"{BASE}{method}?{q}", timeout=30) as r:
        return json.load(r)["result"]


def index_price(cur):  # cur in {BTC, ETH}
    return _get("get_index_price", index_name=f"{cur.lower()}_usd")["index_price"]


def dvol(cur):
    # latest DVOL via 1 recent daily candle
    end = 4102444800000  # far future; API clamps to now
    d = _get("get_volatility_index_data", currency=cur, start_timestamp=end - 5 * 86400000,
             end_timestamp=end, resolution=86400)["data"]
    return d[-1][4] / 100.0 if d else None  # close, as fraction


def perp_funding(cur):
    tk = _get("ticker", instrument_name=f"{cur}-PERPETUAL")
    return tk.get("funding_8h")


def atm_straddle_iv(cur, target_days=30):
    """ATM ~30d straddle: returns mark/bid/ask IV (avg of call+put) and half-spread (vol pts)."""
    idx = index_price(cur)
    insts = _get("get_instruments", currency=cur, kind="option", expired="false")
    exps = sorted(set(i["expiration_timestamp"] for i in insts))
    t0 = exps[0]
    expiry = min(exps, key=lambda e: abs((e - t0) / 86400000 - target_days))
    near = [i for i in insts if i["expiration_timestamp"] == expiry]
    strike = min(set(i["strike"] for i in near), key=lambda k: abs(k - idx))
    out = {}
    for opt in ("C", "P"):
        nm = next(i["instrument_name"] for i in near if i["strike"] == strike
                  and i["instrument_name"].endswith(opt))
        tk = _get("ticker", instrument_name=nm)
        out[opt] = {"bid_iv": tk.get("bid_iv"), "mark_iv": tk.get("mark_iv"), "ask_iv": tk.get("ask_iv")}
    def avg(f):
        vals = [out[o][f] for o in ("C", "P") if out[o][f]]
        return sum(vals) / len(vals) / 100.0 if vals else None
    mark, bid, ask = avg("mark_iv"), avg("bid_iv"), avg("ask_iv")
    half = (ask - bid) / 2.0 if (ask and bid) else None
    days = (expiry - t0) / 86400000
    return {"strike": strike, "expiry_ms": expiry, "days": round(days, 1),
            "mark_iv": mark, "bid_iv": bid, "ask_iv": ask, "half_spread_vp": half}


if __name__ == "__main__":
    for cur in ("BTC", "ETH"):
        s = atm_straddle_iv(cur)
        print(f"{cur}: idx={index_price(cur):.0f} DVOL={dvol(cur)*100:.1f}% funding8h={perp_funding(cur)} "
              f"| ATM{s['days']}d strike={s['strike']} mark_iv={s['mark_iv']*100:.1f}% "
              f"half_spread={s['half_spread_vp']*100:.2f}vp")
