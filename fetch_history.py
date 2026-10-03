"""
fetch_history.py — warm-start the candle store from Binance Vision.

Roostoo prices stream from Binance, and the official Data Sources Pack
recommends data.binance.vision (free bulk history, no auth) for backtests.
This downloads the last ceil(WARMUP_HOURS/24) COMPLETE daily 1h kline files
per pair (up to yesterday — never a partial current day) and stores them as
source='binance'. Live candles always take precedence on conflict.

Re-runnable any time (e.g. to refresh history before the main round).
Usage:  python3 fetch_history.py
"""
from __future__ import annotations

import io
import math
import sys
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

import config as cfg
from candles import CandleStore


def binance_symbol(pair: str) -> str:
    """BTC/USD -> BTCUSDT (Roostoo prices stream from Binance spot)."""
    return pair.replace("/USD", "USDT")


def fetch_day(symbol: str, day: str) -> list[tuple]:
    """Return [(ts_seconds, o, h, l, c), ...] for one daily zip, or []."""
    url = (f"https://data.binance.vision/data/spot/daily/klines/{symbol}/1h/"
           f"{symbol}-1h-{day}.zip")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "roostoo-bot/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except Exception as e:
        print(f"  {symbol} {day}: skipped ({e})")
        return []
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        lines = z.read(z.namelist()[0]).decode().splitlines()
    except Exception as e:
        print(f"  {symbol} {day}: bad zip ({e})")
        return []
    rows = []
    for line in lines:
        f = line.split(",")
        if len(f) < 5 or not f[0].isdigit():
            continue
        ts = int(f[0]) // 1_000_000  # open time in microseconds -> seconds
        rows.append((ts, float(f[1]), float(f[2]), float(f[3]), float(f[4])))
    return rows


def main() -> int:
    store = CandleStore(cfg.DB_PATH)
    n_days = math.ceil(cfg.WARMUP_HOURS / 24)
    today = datetime.now(timezone.utc).date()
    total = 0
    for pair in cfg.PAIRS:
        symbol = binance_symbol(pair)
        before = store.count(pair)
        print(f"{pair} ({symbol}): downloading {n_days} complete daily files...")
        for i in range(n_days, 0, -1):
            day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            rows = fetch_day(symbol, day)
            for ts, o, h, l, c in rows:
                store.upsert_candle(pair, ts, o, h, l, c, 1, source="binance")
            print(f"  {day}: {len(rows)} rows")
        after = store.count(pair)
        total += max(0, after - before)
        print(f"  {pair}: {after} candles now stored (+{after - before})")
    print(f"done. {total} new warm-start candles. "
          f"Need >= {max(120, 96)} for signals (config.WARMUP_HOURS={cfg.WARMUP_HOURS}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
