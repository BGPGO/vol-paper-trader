"""Flask dashboard + background poller for the vol paper trader.

Shows, side by side: the REAL Deribit ATM-30d straddle spread (the make-or-break cost),
and two paper books on R$100k — TAKER (cross the spread) vs MAKER (post at mid and wait),
both running the laddered short-vol BTC+ETH + 1d-cashout strategy.
"""
from __future__ import annotations
import base64
import io
import os
import threading
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from flask import Flask, Response

import store
import strategy

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "3600"))
app = Flask(__name__)
_started = False


def _poller():
    while True:
        try:
            strategy.poll_and_log()
            strategy.decide()
        except Exception as e:  # noqa: BLE001
            print("poll error:", e, flush=True)
        time.sleep(POLL_SECONDS)


def _ensure():
    global _started
    if not _started:
        store.init(); _started = True
        threading.Thread(target=_poller, daemon=True).start()


def _png(fig):
    b = io.BytesIO(); fig.savefig(b, format="png", bbox_inches="tight"); plt.close(fig)
    return f'<img src="data:image/png;base64,{base64.b64encode(b.getvalue()).decode()}"/>'


def _charts():
    out = {}
    t = pd.DataFrame(store.query("SELECT * FROM ticks ORDER BY ts"))
    if not t.empty:
        t["dt"] = pd.to_datetime(t["ts"], unit="s")
        fig, ax = plt.subplots(figsize=(8, 2.7))
        for cur, c in (("BTC", "#1f77b4"), ("ETH", "#ff7f0e")):
            d = t[t["asset"] == cur]
            if not d.empty:
                ax.plot(d["dt"], d["half_spread_vp"] * 100, ".-", ms=3, color=c,
                        label=f"{cur} now {d['half_spread_vp'].iloc[-1]*100:.2f} / med {d['half_spread_vp'].median()*100:.2f} vp")
        ax.axhline(1.0, color="#d62728", ls="--", lw=1, label="1.0 vp")
        ax.set_ylabel("ATM 30d half-spread (vp)"); ax.set_title("REAL option spread (make-or-break cost)")
        ax.legend(fontsize=8); ax.grid(alpha=.25)
        out["spread"] = _png(fig)
    e = pd.DataFrame(store.query("SELECT * FROM equity_log ORDER BY ts"))
    if not e.empty and len(e) > 1:
        e["dt"] = pd.to_datetime(e["ts"], unit="s")
        fig, ax = plt.subplots(figsize=(8, 2.7))
        ax.plot(e["dt"], e["eq_maker"] / 1000, color="#2ca02c", lw=1.8, label="MAKER (post at mid)")
        ax.plot(e["dt"], e["eq_taker"] / 1000, color="#1f77b4", lw=1.5, label="TAKER (cross spread)")
        ax.axhline(100, color="k", lw=.6, ls="--")
        ax.set_ylabel("R$ thousands"); ax.set_title("Paper equity — maker vs taker")
        ax.legend(fontsize=8); ax.grid(alpha=.25)
        out["equity"] = _png(fig)
    return out


@app.route("/")
def home():
    _ensure()
    st, marks = strategy.snapshot()
    ch = _charts()
    ms = st["maker_stats"]
    fillrate = (ms["filled"] / ms["posted"] * 100) if ms["posted"] else 0
    trades = store.query("SELECT * FROM trades ORDER BY ts DESC LIMIT 30")
    n_ticks = store.query("SELECT COUNT(*) n FROM ticks")[0]["n"]

    def card(b, color):
        eq = st["books"][b]["equity"]; ret = (eq / strategy.BANKROLL0 - 1) * 100
        n = len(marks[b])
        return (f"<div class=card style='border-top:3px solid {color}'><div class=muted>{b.upper()}</div>"
                f"<div class=big>R${eq:,.0f}</div><div class=muted>{ret:+.1f}% · {n} open tranches</div></div>")

    def trow(r):
        tm = time.strftime("%m-%d %H:%M", time.gmtime(r["ts"]))
        ror = "" if r["ror"] is None else f"{r['ror']*100:+.1f}%"
        pnl = "" if r["pnl_r"] is None else f"R${r['pnl_r']:,.0f}"
        sk = "" if r["strike_iv"] is None else f"{r['strike_iv']*100:.1f}%"
        return (f"<tr><td>{tm}</td><td>{r['asset']}</td><td>{r['action']}</td><td>{sk}</td>"
                f"<td>{r['days_held'] or ''}</td><td>{ror}</td><td>{pnl}</td><td>{r['note'] or ''}</td></tr>")

    trade_rows = "".join(trow(r) for r in trades) or "<tr><td colspan=8>no trades yet</td></tr>"
    spread = ch.get("spread", "<p class=muted>collecting data…</p>")
    equity = ch.get("equity", "<p class=muted>equity curve builds as cycles complete…</p>")
    html = f"""<!doctype html><html><head><meta charset=utf-8><title>Vol Paper Trader</title>
    <meta http-equiv=refresh content=300>
    <style>body{{font:14px/1.5 system-ui,sans-serif;max-width:920px;margin:22px auto;padding:0 16px;color:#1a1a1a}}
    h1{{font-size:20px}}h2{{font-size:14px;border-bottom:2px solid #1f4e79;padding-bottom:3px;margin-top:26px}}
    table{{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}}
    th,td{{border-bottom:1px solid #e6eaf0;padding:5px 8px;text-align:right}}th:first-child,td:first-child{{text-align:left}}
    img{{width:100%;border:1px solid #eee;border-radius:6px}}.muted{{color:#667}}
    .cards{{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0}}
    .card{{flex:1;min-width:150px;border:1px solid #e6eaf0;border-radius:8px;padding:12px 14px;background:#fff}}
    .big{{font:600 24px ui-monospace,monospace}}</style></head><body>
    <h1>Vol Risk Premium — Paper Trader <span class=muted>(BTC+ETH short-vol + 1d cashout, laddered)</span></h1>
    <div class=cards>
      {card("maker", "#2ca02c")}{card("taker", "#1f77b4")}
      <div class=card><div class=muted>maker fill rate</div><div class=big>{fillrate:.0f}%</div>
        <div class=muted>{ms['filled']}/{ms['posted']} filled · {ms['expired']} missed</div></div>
      <div class=card><div class=muted>sizing · ticks</div><div class=big>f={strategy.F}</div>
        <div class=muted>{n_ticks} ticks · every {POLL_SECONDS//60}min</div></div>
    </div>
    <h2>The spread monitor</h2>{spread}
    <h2>Maker vs taker — paper equity</h2>{equity}
    <p class=muted>TAKER crosses the spread (sells at bid). MAKER posts at mid and waits —
    fills when the market trades through (adverse selection, measured) or via uninformed
    flow (assumed {strategy.P_UNINFORMED_DAILY:.0%}/day). Both ladder {strategy.K_TRANCHES}
    tranches/sleeve (~{strategy.STAGGER_DAYS:.0f}d apart) and exit as takers on cashout.</p>
    <h2>Trade log</h2><table><tr><th>time UTC</th><th>asset</th><th>action</th><th>strike IV</th>
      <th>days</th><th>ROR</th><th>P&amp;L</th><th>note</th></tr>{trade_rows}</table>
    <p class=muted>Paper · variance-swap convention with live measured spread · fixed Deribit fees ·
    cashout = HAR-lite 1d vol proxy. Not investment advice.</p></body></html>"""
    return Response(html, mimetype="text/html")


@app.route("/healthz")
def health():
    return "ok"


if __name__ == "__main__":
    _ensure()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
