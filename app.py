"""Flask dashboard + background poller for the vol paper trader.

Primary deliverable: a live record of the REAL Deribit ATM-30d straddle spread (the
make-or-break cost), alongside a paper-traded equity curve of the strategy on R$100k.
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

import deribit  # noqa: F401  (kept for clarity / health)
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


def _ensure_poller():
    global _started
    if not _started:
        store.init()
        _started = True
        threading.Thread(target=_poller, daemon=True).start()


def _png(fig):
    b = io.BytesIO(); fig.savefig(b, format="png", bbox_inches="tight"); plt.close(fig)
    return base64.b64encode(b.getvalue()).decode()


def _charts():
    t = pd.DataFrame(store.query("SELECT * FROM ticks ORDER BY ts"))
    if t.empty:
        return "", ""
    t["dt"] = pd.to_datetime(t["ts"], unit="s")
    fig, ax = plt.subplots(figsize=(8, 2.8))
    for cur, c in (("BTC", "#1f77b4"), ("ETH", "#ff7f0e")):
        d = t[t["asset"] == cur]
        if not d.empty:
            ax.plot(d["dt"], d["half_spread_vp"] * 100, ".-", ms=3, color=c,
                    label=f"{cur} (now {d['half_spread_vp'].iloc[-1]*100:.2f}, med {d['half_spread_vp'].median()*100:.2f} vp)")
    ax.axhline(1.0, color="#d62728", ls="--", lw=1, label="1.0 vp (breakeven-ish)")
    ax.set_ylabel("ATM 30d half-spread (vol pts)"); ax.set_title("REAL option spread — the make-or-break cost")
    ax.legend(fontsize=8); ax.grid(alpha=.25)
    spread = _png(fig)
    return spread


@app.route("/")
def home():
    _ensure_poller()
    st, marks = strategy.snapshot()
    spread_png = _charts()
    trades = store.query("SELECT * FROM trades ORDER BY ts DESC LIMIT 25")
    n_ticks = store.query("SELECT COUNT(*) n FROM ticks")[0]["n"]
    eq = st["equity"]; ret = (eq / strategy.BANKROLL0 - 1) * 100
    pos_rows = "".join(
        f"<tr><td>{c}</td><td>{m['days']}d</td><td>{m['strike_iv']*100:.1f}%</td>"
        f"<td>{m['mark_ror']*100:+.1f}%</td><td>R${m['mark_pnl_r']:,.0f}</td></tr>"
        for c, m in marks.items()) or "<tr><td colspan=5>flat (waiting for next 30d slot)</td></tr>"
    def _trow(r):
        tm = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["ts"]))
        ror = "" if r["ror"] is None else f"{r['ror']*100:+.1f}%"
        pnl = "" if r["pnl_r"] is None else f"R${r['pnl_r']:,.0f}"
        return (f"<tr><td>{tm}</td><td>{r['asset']}</td><td>{r['action']}</td>"
                f"<td>{r['days_held'] or 0}</td><td>{ror}</td><td>{pnl}</td><td>{r['note'] or ''}</td></tr>")
    trade_rows = "".join(_trow(r) for r in trades) or "<tr><td colspan=7>no trades yet</td></tr>"
    img = f'<img src="data:image/png;base64,{spread_png}"/>' if spread_png else "<p>collecting data…</p>"
    html = f"""<!doctype html><html><head><meta charset=utf-8><title>Vol Paper Trader</title>
    <meta http-equiv=refresh content=300>
    <style>body{{font:14px/1.5 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1a1a1a}}
    h1{{font-size:20px}}h2{{font-size:15px;border-bottom:2px solid #1f4e79;padding-bottom:3px;margin-top:28px}}
    table{{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}}
    th,td{{border-bottom:1px solid #e6eaf0;padding:5px 8px;text-align:right}}th:first-child,td:first-child{{text-align:left}}
    img{{width:100%;border:1px solid #eee;border-radius:6px}}
    .big{{font:600 26px ui-monospace,monospace}}.muted{{color:#667}}.box{{display:flex;gap:30px;margin:14px 0}}</style></head><body>
    <h1>Vol Risk Premium — Paper Trader <span class=muted>(BTC+ETH short-vol + 1d cashout)</span></h1>
    <div class=box>
      <div><div class=muted>equity (R$100k start)</div><div class=big>R${eq:,.0f}</div><div class=muted>{ret:+.1f}%</div></div>
      <div><div class=muted>sizing</div><div class=big>f={strategy.F}</div><div class=muted>~25% DD budget</div></div>
      <div><div class=muted>realized P&amp;L</div><div class=big>R${st['realized_pnl']:,.0f}</div></div>
      <div><div class=muted>ticks logged</div><div class=big>{n_ticks}</div><div class=muted>every {POLL_SECONDS//60}min</div></div>
    </div>
    <h2>The spread monitor (primary goal)</h2>{img}
    <p class=muted>Half-spread of the ATM ~30d straddle in vol points. Net Sharpe of the strategy
    is ~1.6 near 0 vp, ~0.6 at 1 vp, negative at 2 vp — so this chart decides whether the
    strategy is tradeable. Fees are fixed; only the spread is uncertain, so we measure it.</p>
    <h2>Open positions (paper)</h2><table><tr><th>asset</th><th>held</th><th>strike IV</th><th>mark ROR</th><th>mark P&amp;L</th></tr>{pos_rows}</table>
    <h2>Trade log</h2><table><tr><th>time (UTC)</th><th>asset</th><th>action</th><th>days</th><th>ROR</th><th>P&amp;L</th><th>note</th></tr>{trade_rows}</table>
    <p class=muted>Paper trade · variance-swap convention with the live measured spread · cashout = lightweight HAR-lite 1d vol proxy.</p>
    </body></html>"""
    return Response(html, mimetype="text/html")


@app.route("/healthz")
def health():
    return "ok"


if __name__ == "__main__":
    _ensure_poller()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
