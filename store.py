"""SQLite persistence for the paper trader (survives restarts via the /data volume)."""
from __future__ import annotations
import json
import os
import sqlite3
import time

DB = os.environ.get("DB_PATH", "/data/paper.db")


def _conn():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS ticks(
            ts INTEGER, asset TEXT, index_px REAL, dvol REAL, mark_iv REAL,
            bid_iv REAL, ask_iv REAL, half_spread_vp REAL, funding8h REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS trades(
            ts INTEGER, asset TEXT, action TEXT, days_held REAL, strike_iv REAL,
            rv_ann REAL, ror REAL, cost_ror REAL, pnl_r REAL, note TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")


def add_tick(asset, d):
    with _conn() as c:
        c.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?,?)",
                  (int(time.time()), asset, d["index_px"], d["dvol"], d["mark_iv"],
                   d["bid_iv"], d["ask_iv"], d["half_spread_vp"], d["funding8h"]))


def add_trade(asset, action, **kw):
    with _conn() as c:
        c.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (int(time.time()), asset, action, kw.get("days_held"), kw.get("strike_iv"),
                   kw.get("rv_ann"), kw.get("ror"), kw.get("cost_ror"), kw.get("pnl_r"),
                   kw.get("note", "")))


def get_state(default):
    with _conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k='state'").fetchone()
        return json.loads(r["v"]) if r else default


def set_state(s):
    with _conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES('state',?) ON CONFLICT(k) DO UPDATE SET v=?",
                  (json.dumps(s), json.dumps(s)))


def query(sql, args=()):
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
