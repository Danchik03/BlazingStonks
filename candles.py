"""
candles.py — build 1h candles from /v3/ticker polls + SQLite persistence.

Roostoo serves no OHLCV (ticker snapshots only), so we fold every LastPrice
into the open candle bucket and persist every candle to SQLite. That gives us:
  * crash-safe state — a restart resumes the open candle from disk
  * the exact OHLC frame the backtests used, for live signal computation
  * a complete audit trail of every price the bot saw

`source` column: 'live' = built from Roostoo ticker polls; 'binance' =
warm-start history from data.binance.vision (see fetch_history.py).
A 'live' row always beats a 'binance' row for the same bucket.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(
  pair   TEXT NOT NULL,
  ts     INTEGER NOT NULL,            -- bucket start, unix seconds (UTC)
  o REAL, h REAL, l REAL, c REAL,
  n    INTEGER DEFAULT 1,             -- ticks folded in
  source TEXT DEFAULT 'live',         -- 'live' | 'binance'
  PRIMARY KEY(pair, ts)
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, pair TEXT, side TEXT, otype TEXT, qty REAL, price REAL,
  status TEXT, role TEXT, fee REAL, api_order_id TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS decisions(
  ts INTEGER, pair TEXT, target INTEGER, long_votes INTEGER, equity REAL,
  signals TEXT
);
CREATE TABLE IF NOT EXISTS api_log(
  ts INTEGER, endpoint TEXT, ok INTEGER, detail TEXT
);
"""


class CandleStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------- candles
    def upsert_candle(self, pair: str, ts: int, o: float, h: float, l: float,
                      c: float, n: int, source: str = "live") -> None:
        """Insert or update a candle. Live rows win over binance rows."""
        self.conn.execute(
            """
            INSERT INTO candles(pair, ts, o, h, l, c, n, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair, ts) DO UPDATE SET
              o    = CASE WHEN candles.source = 'live'  THEN candles.o  ELSE excluded.o END,
              h    = CASE WHEN candles.source = 'live'  THEN candles.h  ELSE excluded.h END,
              l    = CASE WHEN candles.source = 'live'  THEN candles.l  ELSE excluded.l END,
              c    = CASE WHEN candles.source = 'live'  THEN candles.c  ELSE excluded.c END,
              n    = CASE WHEN candles.source = 'live'  THEN candles.n  ELSE excluded.n END,
              source = CASE WHEN candles.source = 'live' THEN 'live' ELSE excluded.source END
            """,
            (pair, ts, o, h, l, c, n, source),
        )

    def get_candle(self, pair: str, ts: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT o, h, l, c, n, source FROM candles WHERE pair=? AND ts=?",
            (pair, ts)).fetchone()
        if not row:
            return None
        return {"o": row[0], "h": row[1], "l": row[2], "c": row[3],
                "n": row[4], "source": row[5]}

    def load(self, pair: str, hours: int, before_ts: int) -> pd.DataFrame:
        """Last `hours` CLOSED candles (buckets strictly before `before_ts`)
        as the frame roostoo_signals.py expects: columns open/high/low/close."""
        rows = self.conn.execute(
            """
            SELECT ts, o, h, l, c FROM candles
            WHERE pair = ? AND ts < ?
            ORDER BY ts DESC LIMIT ?
            """, (pair, before_ts, hours)).fetchall()
        if len(rows) < 2:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        rows = list(reversed(rows))
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
        df.index = pd.to_datetime(df.pop("ts"), unit="s", utc=True)
        df.index.name = "ts"
        return df

    def count(self, pair: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM candles WHERE pair=?", (pair,)).fetchone()[0])

    # ------------------------------------------------------------------- kv
    def kv_get(self, k: str, default=None):
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]

    def kv_set(self, k: str, v) -> None:
        self.conn.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v)))

    # --------------------------------------------------------------- audit
    def log_order(self, pair: str, side: str, otype: str, qty: float, price: float,
                  status: str, role: str, fee: float, api_order_id: str,
                  note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO orders(ts, pair, side, otype, qty, price, status, role, fee, api_order_id, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), pair, side, otype, qty, price, status, role, fee,
             api_order_id, note))

    def log_decision(self, pair: str, target: int, long_votes: int, equity: float,
                     signals: dict) -> None:
        self.conn.execute(
            "INSERT INTO decisions(ts, pair, target, long_votes, equity, signals)"
            " VALUES(?,?,?,?,?,?)",
            (int(time.time()), pair, target, long_votes, equity, json.dumps(signals)))

    def log_api(self, endpoint: str, ok: bool, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO api_log(ts, endpoint, ok, detail) VALUES(?,?,?,?)",
            (int(time.time()), endpoint, int(ok), detail[:500]))
