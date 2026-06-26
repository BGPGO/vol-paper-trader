"""Flask dashboard + background poller — two tabs:
  /      core universe (BTC, ETH)
  /alts  alts universe (SOL, XRP, HYPE, AVAX) — forward data collection + paper books
Each universe: spread monitor, book-vs-order charts, 3 execution books (taker/maker/chase)
on their own R$100k, marked-to-market equity.
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

import store
import strategy

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
app = Flask(__name__)
_started = False
COLOR = {"taker": "#1f77b4", "maker": "#2ca02c", "half": "#ff7f0e", "chase": "#9467bd"}
DESC = {"taker": "cross spread", "maker": "post mid, can miss",
        "half": "post mid-hs/2, can miss", "chase": "mid→bid"}
ACOLOR = {"BTC": "#1f77b4", "ETH": "#ff7f0e", "SOL": "#2ca02c", "XRP": "#9467bd",
          "HYPE": "#e377c2", "AVAX": "#d62728"}


def _fmt_x(ax):
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    for lb in ax.get_xticklabels():
        lb.set_rotation(0); lb.set_fontsize(7)


def _poller():
    while True:
        try:
            strategy.poll_and_log(); strategy.decide()
        except Exception as e:  # noqa: BLE001
            print("poll error:", e, flush=True)
        time.sleep(POLL_SECONDS)


def _ensure():
    global _started
    if not _started:
        store.init()
        store.reset_if_epoch(os.environ.get("DATA_EPOCH", "1"))
        _started = True
        threading.Thread(target=_poller, daemon=True).start()


def _png(fig):
    b = io.BytesIO(); fig.savefig(b, format="png", bbox_inches="tight"); plt.close(fig)
    return f'<img src="data:image/png;base64,{base64.b64encode(b.getvalue()).decode()}"/>'


def _spread_chart(assets):
    t = pd.DataFrame(store.query("SELECT * FROM ticks ORDER BY ts"))
    t = t[t["asset"].isin(assets)] if not t.empty else t
    if t.empty or t["ts"].nunique() < 2:
        return None
    t["dt"] = pd.to_datetime(t["ts"], unit="s")
    fig, ax = plt.subplots(figsize=(8, 2.7))
    for cur in assets:
        d = t[t["asset"] == cur]
        if not d.empty:
            ax.plot(d["dt"], d["half_spread_vp"] * 100, ".-", ms=3, color=ACOLOR.get(cur),
                    label=f"{cur} now {d['half_spread_vp'].iloc[-1]*100:.2f} / med {d['half_spread_vp'].median()*100:.2f} vp")
    ax.axhline(1.0, color="#d62728", ls="--", lw=1, label="1.0 vp")
    ax.set_ylabel("ATM 30d half-spread (vp)"); ax.set_title("Spread real das opções (custo make-or-break)")
    ax.legend(fontsize=7.5, ncol=2); ax.grid(alpha=.25); _fmt_x(ax)
    return _png(fig)


def _equity_chart(universe):
    e = pd.DataFrame(store.query("SELECT * FROM equity_pts ORDER BY ts"))
    if e.empty:
        return None
    e = e[e["book"].str.startswith(universe + ":")]
    if e.empty or e["ts"].nunique() < 2:
        return None
    e["dt"] = pd.to_datetime(e["ts"], unit="s")
    fig, ax = plt.subplots(figsize=(8, 2.7))
    labels = {"taker": "TAKER (cross)", "maker": "MAKER (post mid)",
              "half": "HALF (mid-hs/2)", "chase": "CHASE (mid→bid)"}
    for b in strategy.BOOKS:
        d = e[e["book"] == f"{universe}:{b}"]
        if not d.empty:
            ax.plot(d["dt"], d["equity"] / 1000, color=COLOR[b], lw=1.7, label=labels[b])
    ax.axhline(100, color="k", lw=.6, ls="--")
    ax.set_ylabel("R$ mil (cada book = R$100k próprio)")
    ax.set_title("Equity paper — estilos de execução"); ax.legend(fontsize=8); ax.grid(alpha=.25); _fmt_x(ax)
    return _png(fig)


def _book_chart(asset):
    t = pd.DataFrame(store.query("SELECT * FROM ticks WHERE asset=? ORDER BY ts", (asset,)))
    if t.empty or t["ts"].nunique() < 2:
        return None
    t["dt"] = pd.to_datetime(t["ts"], unit="s"); t_end = t["dt"].iloc[-1]
    fig, ax = plt.subplots(figsize=(8, 3.0))
    ax.fill_between(t["dt"], t["bid_iv"] * 100, t["ask_iv"] * 100, color="#9aa7b8", alpha=0.28, label="bid–ask (spread)")
    ax.plot(t["dt"], t["mark_iv"] * 100, color="#1f4e79", lw=0.9, ls="--", label="mid do mercado (se mexe)")
    ev = pd.DataFrame(store.query("SELECT * FROM trades WHERE asset=? AND strike_iv IS NOT NULL ORDER BY ts", (asset,)))
    if not ev.empty:
        ev["dt"] = pd.to_datetime(ev["ts"], unit="s")
        for book, col in (("maker", "#2ca02c"), ("half", "#ff7f0e"), ("chase", "#9467bd")):
            resol = {"FILL-chase", "CROSS-chase"} if book == "chase" else {f"FILL-{book}", f"EXPIRE-{book}"}
            open_posts, first = [], True
            for _, r in ev.iterrows():
                if r["action"] == f"POST-{book}":
                    open_posts.append((r["dt"], r["strike_iv"]))
                elif r["action"] in resol and open_posts:
                    pdt, pk = open_posts.pop(0)
                    ax.hlines(pk * 100, pdt, r["dt"], color=col, lw=2.4, alpha=.8,
                              label=f"ordem {book} (parada)" if first else None); first = False
            for pdt, pk in open_posts:
                ax.hlines(pk * 100, pdt, t_end, color=col, lw=2.4, ls=":", alpha=.8,
                          label=f"ordem {book} (parada)" if first else None); first = False
        styles = {"FILL-maker": ("^", "#2ca02c", "fill maker"), "FILL-half": ("^", "#ff7f0e", "fill half"),
                  "FILL-chase": ("^", "#9467bd", "fill chase"),
                  "CROSS-chase": ("x", "#d62728", "chase→bid"), "ENTER-taker": ("v", "#1f77b4", "taker")}
        for act, (mk, col, lab) in styles.items():
            d = ev[ev["action"] == act]
            if not d.empty:
                ax.scatter(d["dt"], d["strike_iv"] * 100, marker=mk, c=col, s=45, label=lab, zorder=6)
    ax.set_ylabel("IV (vol %)"); ax.set_title(f"{asset}: book do mercado vs nossa ordem parada & execuções")
    ax.legend(fontsize=6.5, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.18)); ax.grid(alpha=.2); _fmt_x(ax)
    return _png(fig)


def render(universe):
    _ensure()
    assets = strategy.UNIVERSES[universe]
    uni, marks, marked = strategy.snapshot(universe)
    mst, hst, cst = uni["stats"]["maker"], uni["stats"]["half"], uni["stats"]["chase"]
    mk_fill = (mst["filled"] / mst["posted"] * 100) if mst["posted"] else 0
    hf_fill = (hst["filled"] / hst["posted"] * 100) if hst["posted"] else 0
    qs = ",".join(f"'{a}'" for a in assets)
    trades = store.query(f"SELECT * FROM trades WHERE asset IN ({qs}) ORDER BY ts DESC LIMIT 30")
    n_ticks = store.query(f"SELECT COUNT(*) n FROM ticks WHERE asset IN ({qs})")[0]["n"]

    def card(b):
        eq = marked[b]; ret = (eq / strategy.BANKROLL0 - 1) * 100; n = len(marks[b])
        return (f"<div class=card style='border-top:3px solid {COLOR[b]}'>"
                f"<div class=muted>{b.upper()} <span style='font-weight:400'>· {DESC[b]}</span></div>"
                f"<div class=big>R${eq:,.0f}</div><div class=muted>{ret:+.1f}% · {n} tranches · R$100k próprio</div></div>")

    def trow(r):
        tm = time.strftime("%m-%d %H:%M", time.gmtime(r["ts"]))
        ror = "" if r["ror"] is None else f"{r['ror']*100:+.1f}%"
        pnl = "" if r["pnl_r"] is None else f"R${r['pnl_r']:,.0f}"
        sk = "" if r["strike_iv"] is None else f"{r['strike_iv']*100:.1f}%"
        return (f"<tr><td>{tm}</td><td>{r['asset']}</td><td>{r['action']}</td><td>{sk}</td>"
                f"<td>{r['days_held'] or ''}</td><td>{ror}</td><td>{pnl}</td><td>{r['note'] or ''}</td></tr>")

    spread = _spread_chart(assets) or "<p class=muted>coletando… (precisa ≥2 ticks)</p>"
    equity = _equity_chart(universe) or "<p class=muted>curva de equity aparece com mais ciclos…</p>"
    books_html = "".join(_book_chart(a) or f"<p class=muted>{a}: coletando…</p>" for a in assets)
    trade_rows = "".join(trow(r) for r in trades) or "<tr><td colspan=8>sem trades ainda</td></tr>"
    cards = "".join(card(b) for b in strategy.BOOKS)
    other = "alts" if universe == "core" else "core"
    other_lbl = "Alts (SOL/XRP/HYPE/AVAX) →" if universe == "core" else "← Core (BTC/ETH)"
    note = ("" if universe == "core" else
            "<p class=muted style='background:#fbf1df;padding:8px;border-radius:6px'>"
            "<b>Alts = coleta forward.</b> Não há DVOL/histórico pra estes — este universo existe pra "
            "<b>acumular IV/spread/RV ao longo do tempo</b> e tornar um backtest possível no futuro. "
            "Opções USDC-lineares, finas: spreads largos esperados.</p>")
    html = f"""<!doctype html><html><head><meta charset=utf-8><title>Vol Paper — {universe}</title>
    <meta http-equiv=refresh content=300>
    <style>body{{font:14px/1.5 system-ui,sans-serif;max-width:920px;margin:22px auto;padding:0 16px;color:#1a1a1a}}
    h1{{font-size:20px}}h2{{font-size:14px;border-bottom:2px solid #1f4e79;padding-bottom:3px;margin-top:26px}}
    table{{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}}
    th,td{{border-bottom:1px solid #e6eaf0;padding:5px 8px;text-align:right}}th:first-child,td:first-child{{text-align:left}}
    img{{width:100%;border:1px solid #eee;border-radius:6px}}.muted{{color:#667}}
    .cards{{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0}}
    .card{{flex:1;min-width:150px;border:1px solid #e6eaf0;border-radius:8px;padding:12px 14px;background:#fff}}
    .big{{font:600 22px ui-monospace,monospace}} .tabs a{{margin-right:14px;font-weight:600;text-decoration:none;color:#1f4e79}}
    .tabs a.on{{border-bottom:3px solid #1f4e79}}</style></head><body>
    <div class=tabs><a href='/' class='{"on" if universe=="core" else ""}'>Core BTC/ETH</a>
      <a href='/alts' class='{"on" if universe=="alts" else ""}'>Alts SOL/XRP/HYPE/AVAX</a></div>
    <h1>Vol Paper Trader — universo <b>{universe}</b> <span class=muted>(short-vol + cashout CatBoost 1d, laddered)</span></h1>
    {note}
    <div class=cards>{cards}</div>
    <div class=cards>
      <div class=card><div class=muted>MAKER fills (mid)</div><div class=big>{mk_fill:.0f}%</div>
        <div class=muted>{mst['filled']}/{mst['posted']} · {mst['expired']} missed</div></div>
      <div class=card style='border-top:3px solid #ff7f0e'><div class=muted>HALF fills (mid-hs/2)</div><div class=big>{hf_fill:.0f}%</div>
        <div class=muted>{hst['filled']}/{hst['posted']} · {hst['expired']} missed</div></div>
      <div class=card><div class=muted>CHASE fills</div><div class=big>{cst['filled']}+{cst['crossed']}</div>
        <div class=muted>{cst['filled']} mid · {cst['crossed']} crossed · {cst['posted']} posted</div></div>
      <div class=card><div class=muted>sizing · ticks</div><div class=big>f={strategy.F}</div>
        <div class=muted>{n_ticks} ticks · {POLL_SECONDS//60}min</div></div>
    </div>
    <h2>Spread monitor</h2>{spread}
    <h2>Book vs nossa ordem (por ativo)</h2>{books_html}
    <h2>Equity dos books</h2>{equity}
    <h2>Trade log</h2><table><tr><th>hora UTC</th><th>ativo</th><th>ação</th><th>strike IV</th>
      <th>dias</th><th>ROR</th><th>P&amp;L</th><th>nota</th></tr>{trade_rows}</table>
    <p class=muted>Paper · spread medido ao vivo · fills do tape real Deribit · cashout = CatBoost 1d (retreina diário). Não é conselho.</p>
    </body></html>"""
    return Response(html, mimetype="text/html")


@app.route("/")
def home():
    return render("core")


@app.route("/alts")
def alts():
    return render("alts")


@app.route("/healthz")
def health():
    return "ok"


if __name__ == "__main__":
    _ensure()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
