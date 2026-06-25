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
import matplotlib.dates as mdates
import pandas as pd
from flask import Flask, Response


def _fmt_x(ax):
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    for lb in ax.get_xticklabels():
        lb.set_rotation(0); lb.set_fontsize(7)

import store
import strategy

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
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
        store.init()
        store.reset_if_epoch(os.environ.get("DATA_EPOCH", "1"))  # clean restart when bumped
        _started = True
        threading.Thread(target=_poller, daemon=True).start()


def _png(fig):
    b = io.BytesIO(); fig.savefig(b, format="png", bbox_inches="tight"); plt.close(fig)
    return f'<img src="data:image/png;base64,{base64.b64encode(b.getvalue()).decode()}"/>'


def _charts():
    out = {}
    t = pd.DataFrame(store.query("SELECT * FROM ticks ORDER BY ts"))
    if not t.empty and t["ts"].nunique() >= 2:
        t["dt"] = pd.to_datetime(t["ts"], unit="s")
        fig, ax = plt.subplots(figsize=(8, 2.7))
        for cur, c in (("BTC", "#1f77b4"), ("ETH", "#ff7f0e")):
            d = t[t["asset"] == cur]
            if not d.empty:
                ax.plot(d["dt"], d["half_spread_vp"] * 100, ".-", ms=3, color=c,
                        label=f"{cur} now {d['half_spread_vp'].iloc[-1]*100:.2f} / med {d['half_spread_vp'].median()*100:.2f} vp")
        ax.axhline(1.0, color="#d62728", ls="--", lw=1, label="1.0 vp")
        ax.set_ylabel("ATM 30d half-spread (vp)"); ax.set_title("REAL option spread (make-or-break cost)")
        ax.legend(fontsize=8); ax.grid(alpha=.25); _fmt_x(ax)
        out["spread"] = _png(fig)
    e = pd.DataFrame(store.query("SELECT * FROM equity_pts ORDER BY ts"))
    if not e.empty and e["ts"].nunique() > 1:
        e["dt"] = pd.to_datetime(e["ts"], unit="s")
        fig, ax = plt.subplots(figsize=(8, 2.7))
        colors = {"taker": "#1f77b4", "maker": "#2ca02c", "chase": "#9467bd"}
        labels = {"taker": "TAKER (cross)", "maker": "MAKER (post mid, can miss)", "chase": "CHASE (mid→bid)"}
        for b in ("taker", "maker", "chase"):
            d = e[e["book"] == b]
            if not d.empty:
                ax.plot(d["dt"], d["equity"] / 1000, color=colors[b], lw=1.7, label=labels[b])
        ax.axhline(100, color="k", lw=.6, ls="--")
        ax.set_ylabel("R$ thousands (each book = own R$100k)")
        ax.set_title("Paper equity — execution styles head-to-head")
        ax.legend(fontsize=8); ax.grid(alpha=.25); _fmt_x(ax)
        out["equity"] = _png(fig)
    return out


def _book_chart(asset):
    t = pd.DataFrame(store.query("SELECT * FROM ticks WHERE asset=? ORDER BY ts", (asset,)))
    if t.empty or t["ts"].nunique() < 2:   # need ≥2 points or the band/line can't draw
        return None
    t["dt"] = pd.to_datetime(t["ts"], unit="s")
    t_end = t["dt"].iloc[-1]
    fig, ax = plt.subplots(figsize=(8, 3.0))
    # the BOOK: shaded gap between best bid and best ask = the spread (no resting orders here)
    ax.fill_between(t["dt"], t["bid_iv"] * 100, t["ask_iv"] * 100, color="#9aa7b8", alpha=0.28,
                    label="bid–ask (spread: vazio)")
    ax.plot(t["dt"], t["ask_iv"] * 100, color="#9aa7b8", lw=0.6)
    ax.plot(t["dt"], t["bid_iv"] * 100, color="#9aa7b8", lw=0.6)
    # the MARKET mid (moves as the market re-quotes) — NOT our order
    ax.plot(t["dt"], t["mark_iv"] * 100, color="#1f4e79", lw=0.9, ls="--", label="mid do mercado (se mexe)")

    ev = pd.DataFrame(store.query(
        "SELECT * FROM trades WHERE asset=? AND strike_iv IS NOT NULL ORDER BY ts", (asset,)))
    if not ev.empty:
        ev["dt"] = pd.to_datetime(ev["ts"], unit="s")
        # OUR resting orders: a FIXED horizontal segment at the posted price, from post-time
        # until it filled / expired / crossed (or to now if still resting).
        for book, col in (("maker", "#2ca02c"), ("chase", "#9467bd")):
            resol = {f"FILL-{book}", "EXPIRE-maker" if book == "maker" else "CROSS-chase"}
            open_posts, first = [], True
            for _, r in ev.iterrows():
                if r["action"] == f"POST-{book}":
                    open_posts.append((r["dt"], r["strike_iv"]))
                elif r["action"] in resol and open_posts:
                    pdt, pk = open_posts.pop(0)
                    ax.hlines(pk * 100, pdt, r["dt"], color=col, lw=2.4, alpha=.8,
                              label=f"ordem {book} (parada)" if first else None); first = False
            for pdt, pk in open_posts:  # still resting -> dotted to now
                ax.hlines(pk * 100, pdt, t_end, color=col, lw=2.4, ls=":", alpha=.8,
                          label=f"ordem {book} (parada)" if first else None); first = False
        # execution markers (where it actually filled)
        styles = {"FILL-maker": ("^", "#2ca02c", "fill maker"), "FILL-chase": ("^", "#9467bd", "fill chase"),
                  "CROSS-chase": ("x", "#d62728", "chase cruzou→bid"), "ENTER-taker": ("v", "#1f77b4", "entrada taker")}
        for act, (mk, col, lab) in styles.items():
            d = ev[ev["action"] == act]
            if not d.empty:
                ax.scatter(d["dt"], d["strike_iv"] * 100, marker=mk, c=col, s=45, label=lab, zorder=6)
    ax.set_ylabel("IV (vol %)")
    ax.set_title(f"{asset}: book do mercado (banda) vs nossa ordem parada (linha fixa) & execuções")
    ax.legend(fontsize=6.5, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.18))
    ax.grid(alpha=.2); _fmt_x(ax)
    return _png(fig)


@app.route("/")
def home():
    _ensure()
    st, marks, marked = strategy.snapshot()
    ch = _charts()
    mst, cst = st["stats"]["maker"], st["stats"]["chase"]
    mk_fill = (mst["filled"] / mst["posted"] * 100) if mst["posted"] else 0
    trades = store.query("SELECT * FROM trades ORDER BY ts DESC LIMIT 30")
    n_ticks = store.query("SELECT COUNT(*) n FROM ticks")[0]["n"]
    COLOR = {"taker": "#1f77b4", "maker": "#2ca02c", "chase": "#9467bd"}
    DESC = {"taker": "cross spread", "maker": "post mid, can miss", "chase": "mid→bid"}

    def card(b):
        eq = marked[b]; ret = (eq / strategy.BANKROLL0 - 1) * 100  # marked-to-market (live)
        n = len(marks[b])
        return (f"<div class=card style='border-top:3px solid {COLOR[b]}'>"
                f"<div class=muted>{b.upper()} <span style='font-weight:400'>· {DESC[b]}</span></div>"
                f"<div class=big>R${eq:,.0f}</div><div class=muted>{ret:+.1f}% · {n} tranches · own R$100k</div></div>")

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
    bk = {a: _book_chart(a) for a in strategy.ASSETS}  # already full <img> tags
    books_html = "".join(
        bk[a] if bk[a] else f"<p class=muted>{a}: collecting… (needs ≥2 ticks)</p>"
        for a in strategy.ASSETS)
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
      {card("taker")}{card("maker")}{card("chase")}
    </div>
    <div class=cards>
      <div class=card><div class=muted>MAKER fills</div><div class=big>{mk_fill:.0f}%</div>
        <div class=muted>{mst['filled']}/{mst['posted']} filled · {mst['expired']} missed</div></div>
      <div class=card><div class=muted>CHASE fills</div>
        <div class=big>{cst['filled']}+{cst['crossed']}</div>
        <div class=muted>{cst['filled']} at mid · {cst['crossed']} crossed to bid · {cst['posted']} posted</div></div>
      <div class=card><div class=muted>sizing · ticks</div><div class=big>f={strategy.F}</div>
        <div class=muted>{n_ticks} ticks · every {POLL_SECONDS//60}min</div></div>
    </div>
    <h2>The spread monitor</h2>{spread}
    <h2>Order book over time — where our order sits vs the market</h2>
    <p class=muted>Shaded band = the live bid–ask of the ATM straddle (the book). The line
    is the mid, where MAKER/CHASE rest their offers. Markers show actual fills: maker/chase
    fills (▲), chase crossing to the bid (✕), taker entries (▼) — so you see whether the
    market came to our resting order or we had to cross.</p>
    {books_html}
    <h2>Execution styles — paper equity</h2>{equity}
    <p class=muted><b>Three independent books, each on its own R$100k.</b> TAKER crosses
    the spread (sells at bid, always fills). MAKER posts at mid and waits — fills only when
    a <b>real buy trade prints through the level on Deribit's tape</b> (tick-accurate, no
    aliasing, no assumed prob); if no buyer in {strategy.FILL_WINDOW_DAYS}d it EXPIRES
    (missed). CHASE posts at mid too, but if unfilled by {strategy.CHASE_DEADLINE_DAYS}d it
    CROSSES to the bid (taker) — never misses, capped at taker cost. All ladder
    {strategy.K_TRANCHES} tranches/sleeve (~{strategy.STAGGER_DAYS:.0f}d apart), exit as
    takers on cashout.</p>
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
