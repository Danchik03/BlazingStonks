"""
config.py — ALL the variables, in one place, in plain sight.

Edit this file (commit the change — judges read the history) or override any
scalar via environment variable of the same name, e.g. `DRY_RUN=true`.

CREDENTIALS ARE NOT HERE. Keys live in .env (gitignored, never committed) and
Telegram creds are in .env too. This file only holds trading decisions.

The "variables" that matter, by group:
  1. WHAT WE TRADE    PAIRS, STRATEGIES, MIN_AGREEMENT, SHORTS_ENABLED
  2. MONEY            PER_PAIR_PCT, MAX_TOTAL_EXPOSURE_PCT, DAILY_LOSS_LIMIT_PCT
  3. TIMING           CANDLE_MINUTES, POLL_SECONDS, WARMUP_HOURS
  4. EXECUTION        MAKER_FIRST, MAKER_WAIT_MINUTES, EXITS_MARKET
  5. OPS              DRY_RUN, DB_PATH, LOG_FILE
"""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _e(name: str, default):
    """Scalar env override: unset/empty -> default; otherwise cast to type of default."""
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if isinstance(default, bool):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(v)
    if isinstance(default, float):
        return float(v)
    return v


# =============================================================== CREDENTIALS
# Loaded from .env by roostoo_client.py. NEVER hardcode keys in this file or
# anywhere that gets committed (the repo's commit history is reviewed).
BASE_URL = _e("RST_BASE_URL", "https://mock-api.roostoo.com")

# ============================================================ WHAT WE TRADE
# Pair names must match /v3/exchangeInfo exactly.
PAIRS = [
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
]

# The five MCPT-validated long/flat strategies: (name, params).
# roostoo_signals.py documents what each does and the full validation trail
# (in-sample MCPT, walk-forward, walk-forward MCPT). To remove one, delete the
# line; to add one, copy a name from roostoo_signals.SIGNALS.
STRATEGIES = [
    ("ts_mom_hyst_lf",   {"n": 72,  "d": 0.01}),    # 72h momentum, +/-1% deadband
    ("atr_channel_lf",   {"k": 1.5, "period": 20}), # close > SMA20 + 1.5*ATR20
    ("ma_cross_lf",      {"f": 12,  "s": 96}),      # SMA12 above SMA96
    ("ma_cross_hyst_lf", {"f": 12,  "s": 96, "d": 0.003}),  # same, 0.3% deadband
    ("rsi_trend_lf",     {"p": 14,  "lo": 30, "hi": 70}),   # RSI>70 long, <30 flat
]

# Go long when at least this many of the enabled strategies say long.
# 2/5 = the position is a committee decision, not one indicator's whim.
MIN_AGREEMENT = _e("MIN_AGREEMENT", 2)

# Long/short upgrade: set True only if /v6 shorting is enabled for the
# competition (verified WORKING on the test key, 2026-10-03). With this OFF,
# no /v6 endpoint is ever called — the whole short risk disappears.
SHORTS_ENABLED = _e("SHORTS_ENABLED", False)

# ==================================================================== MONEY
# Fraction of current equity that one pair's long position may use.
# 3 pairs x 20% = 60% deployed, 40% cash buffer.
PER_PAIR_PCT = _e("PER_PAIR_PCT", 0.20)

# Hard cap on total deployed (coin value) across all pairs.
MAX_TOTAL_EXPOSURE_PCT = _e("MAX_TOTAL_EXPOSURE_PCT", 0.95)

# Risk overlay: if intraday equity (since UTC midnight) falls by this much,
# the bot flattens everything, cancels entries, and stays flat until the
# next UTC day. (3% of $1M = $30k — big enough to never fire on noise,
# small enough to matter.)
DAILY_LOSS_LIMIT_PCT = _e("DAILY_LOSS_LIMIT_PCT", 0.03)

# =================================================================== TIMING
# Signals run on closed 1h candles (the timeframe they were validated on).
CANDLE_MINUTES = _e("CANDLE_MINUTES", 60)

# Ticker poll cadence. One call returns ALL pairs, so 60s = 1 of the
# 30 calls/min budget. (Set to 5 for plumbing smoke tests only.)
POLL_SECONDS = _e("POLL_SECONDS", 60)

# Hours of candle history the signal frame needs. Must cover the slowest
# indicator (SMA96 = 96h) with buffer; warm-start is fetched from
# data.binance.vision by fetch_history.py.
WARMUP_HOURS = _e("WARMUP_HOURS", 240)

# ================================================================= EXECUTION
# Enter with a LIMIT order at the spread's maker side (0.05% fee) and give it
# MAKER_WAIT_MINUTES to fill; if still pending, cancel and cross to market
# (0.1%). At a 1h timeframe, occasionally paying 2x the fee to not miss the
# hour is the right trade.
MAKER_FIRST = _e("MAKER_FIRST", True)
MAKER_WAIT_MINUTES = _e("MAKER_WAIT_MINUTES", 10)

# Exits always go to market: getting out outranks saving the fee differential.
EXITS_MARKET = _e("EXITS_MARKET", True)

# ======================================================================= OPS
# DRY_RUN=true: full loop runs (polling, candles, signals, decisions,
# Telegram), but orders are simulated at the spread and logged, not sent.
# ALWAYS run `DRY_RUN=true python3 roostoo_bot.py` once before going live.
DRY_RUN = _e("DRY_RUN", False)

# SQLite audit DB + human-readable log. Both are cheap on the 30GB EBS.
DB_PATH = _e("ROOSTOO_DB", str(HERE / "bot.db"))
LOG_FILE = _e("ROOSTOO_LOG", str(HERE / "bot.log"))

# Push a Telegram ping on every fill/decision (in addition to the periodic
# monitor.py snapshots).
TG_NOTIFY_ON_TRADE = _e("TG_NOTIFY_ON_TRADE", True)
