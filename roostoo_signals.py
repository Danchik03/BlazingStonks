"""
roostoo_signals.py — 5 MCPT-validated strategies for the Roostoo hackathon bot.

Revised after the official FAQ/API docs (Sep 18 info session):
  * Roostoo prices are streamed from Binance  -> backtest on Binance data
    (data.binance.vision, free bulk history), NOT Coinbase.
  * The Roostoo API has NO OHLCV — only ticker snapshots (all pairs in 1 call).
    The bot must build candles locally from tickers (or pull Binance klines).
  * Rate limit: 30 API calls/min INCLUDING queries.
  * Shorting is a separate collateral-based mechanism (/v6/short_open, min $1
    collateral, 0.1% fee both sides, close is always market, and the competition
    MAY disallow it — check with your test key first).
  * First trade must execute by Oct 1 8pm. Pairs look like "BTC/USD".

RECOMMENDED SETUP — long/flat core (no short endpoints needed):

  strategy          params              what it does
  ts_mom_hyst_lf    n=72, d=0.01        long when price > +1% vs 72h ago,
                                        flat when < -1%, hold in between
  atr_channel_lf    k=1.5 (20h)         long on close > SMA20 + 1.5*ATR20,
                                        flat when close < SMA20 - 1.5*ATR20
  ma_cross_lf       f=12, s=96          long when SMA12 > SMA96, else flat
  ma_cross_hyst_lf  f=12, s=96, d=0.003 same cross with 0.3% deadband
  rsi_trend_lf      p=14, lo=30, hi=70  long when RSI(14) > 70, flat when
                                        RSI(14) < 30 (NOT short)

All five: in-sample MCPT p = 0.01 on BTC and ETH; atr_channel_lf and
ts_mom_hyst_lf also pass walk-forward MCPT at ~5% (0.049 / 0.066).
Net-of-fee walk-forward Sharpe (BTC 2020->2026, full size):
  ts_mom_hyst_lf 0.86 | atr_channel_lf 0.78 | ma_cross_lf 0.69 |
  ma_cross_hyst_lf 0.64 | rsi_trend_lf 0.50

OPTIONAL UPGRADE (only if /v6 shorts work in this competition):
  replace rsi_trend_lf with rsi_trend (long/short): the RSI<30 short leg adds
  ~+0.30 net Sharpe (0.50 -> 0.80) and its WF-MCPT p improves to 0.030.

Validation pipeline: ../mcpt_screen/ (4-step MCPT framework, re-runnable).
All signals are strictly causal: position after bar t uses data up to bar t's
close and earns bar t+1's return.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEE_TAKER = 0.001    # Roostoo market order, per side
FEE_MAKER = 0.0005   # Roostoo limit (maker) order, per side
HOURS_PER_YEAR = 24 * 365
# Validated walk-forward schedule (bars = 1h)
WF_TRAIN_WINDOW = 4 * 24 * 365   # 4 years
WF_REOPT_STEP = 90 * 24          # re-optimize every 90 days


# ------------------------------------------------------------------ signals
def rsi(close: pd.Series, period: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).rolling(period).mean()
    dn = (-d.clip(upper=0)).rolling(period).mean()
    return 100 - 100 / (1 + up / (dn + 1e-12))


def rsi_trend_signal(ohlc: pd.DataFrame, p: int = 14, lo: float = 30, hi: float = 70) -> pd.Series:
    """LONG/SHORT version (optional upgrade): RSI>hi long, RSI<lo short."""
    r = rsi(ohlc["close"], p)
    ev = pd.Series(np.nan, index=ohlc.index)
    ev[r > hi] = 1.0
    ev[r < lo] = -1.0
    return ev.ffill().fillna(0.0)


def rsi_trend_lf_signal(ohlc: pd.DataFrame, p: int = 14, lo: float = 30, hi: float = 70) -> pd.Series:
    """Long/flat: RSI>hi long, RSI<lo flat (no short)."""
    return rsi_trend_signal(ohlc, p, lo, hi).clip(lower=0.0)


def ma_cross_lf_signal(ohlc: pd.DataFrame, f: int = 12, s: int = 96) -> pd.Series:
    c = ohlc["close"]
    return (c.rolling(f).mean() > c.rolling(s).mean()).astype(float).fillna(0.0)


def ma_cross_hyst_lf_signal(ohlc: pd.DataFrame, f: int = 12, s: int = 96, d: float = 0.003) -> pd.Series:
    fv = ohlc["close"].rolling(f).mean()
    sv = ohlc["close"].rolling(s).mean()
    ev = pd.Series(np.nan, index=ohlc.index)
    ev[fv > sv * (1 + d)] = 1.0    # enter long
    ev[fv < sv * (1 - d)] = 0.0    # exit long
    return ev.ffill().fillna(0.0)


def _atr(ohlc: pd.DataFrame, period: int = 20) -> pd.Series:
    return (ohlc["high"] - ohlc["low"]).rolling(period).mean()


def atr_channel_signal(ohlc: pd.DataFrame, k: float = 1.5, period: int = 20) -> pd.Series:
    """LONG/SHORT version (k chosen per asset by optimize())."""
    c = ohlc["close"]
    m = c.rolling(period).mean()
    a = _atr(ohlc, period)
    ev = pd.Series(np.nan, index=ohlc.index)
    ev[c > m + k * a] = 1.0
    ev[c < m - k * a] = -1.0
    return ev.ffill().fillna(0.0)


def atr_channel_lf_signal(ohlc: pd.DataFrame, k: float = 1.5, period: int = 20) -> pd.Series:
    return atr_channel_signal(ohlc, k, period).clip(lower=0.0)


def ts_mom_hyst_signal(ohlc: pd.DataFrame, n: int = 72, d: float = 0.01) -> pd.Series:
    """LONG/SHORT version: momentum with deadband."""
    m = ohlc["close"] / ohlc["close"].shift(n) - 1.0
    ev = pd.Series(np.nan, index=ohlc.index)
    ev[m > d] = 1.0
    ev[m < -d] = -1.0
    return ev.ffill().fillna(0.0)


def ts_mom_hyst_lf_signal(ohlc: pd.DataFrame, n: int = 72, d: float = 0.01) -> pd.Series:
    return ts_mom_hyst_signal(ohlc, n, d).clip(lower=0.0)


# The recommended long/flat set (primary bot, no /v6 short endpoints needed)
SIGNALS = {
    "ts_mom_hyst_lf": ts_mom_hyst_lf_signal,
    "atr_channel_lf": atr_channel_lf_signal,
    "ma_cross_lf": ma_cross_lf_signal,
    "ma_cross_hyst_lf": ma_cross_hyst_lf_signal,
    "rsi_trend_lf": rsi_trend_lf_signal,
}

# Optional upgrade set (only if the competition allows /v6 short positions)
SIGNALS_SHORT = {
    "rsi_trend": rsi_trend_signal,
    "atr_channel": atr_channel_signal,
    "ts_mom_hyst": ts_mom_hyst_signal,
}

DEFAULT_PARAMS = {
    "ts_mom_hyst_lf": {"n": 72, "d": 0.01},
    "atr_channel_lf": {"k": 1.5, "period": 20},
    "ma_cross_lf": {"f": 12, "s": 96},
    "ma_cross_hyst_lf": {"f": 12, "s": 96, "d": 0.003},
    "rsi_trend_lf": {"p": 14, "lo": 30, "hi": 70},
}

GRIDS = {
    "ts_mom_hyst_lf": [{"n": n, "d": d} for n in (72, 168, 360, 720)
                       for d in (0.01, 0.02, 0.03)],
    "atr_channel_lf": [{"k": k, "period": 20} for k in (1.5, 2.0, 2.5, 3.0)],
    "ma_cross_lf": [{"f": f, "s": s} for f, s in
                    [(12, 48), (12, 96), (24, 96), (24, 192), (48, 192),
                     (48, 384), (96, 384), (96, 768)]],
    "ma_cross_hyst_lf": [{"f": f, "s": s, "d": d} for f, s, d in
                         [(12, 48, 0.003), (12, 96, 0.003), (12, 96, 0.006),
                          (24, 96, 0.003), (24, 192, 0.003), (24, 192, 0.006),
                          (48, 192, 0.006), (48, 384, 0.01)]],
    "rsi_trend_lf": [{"p": p, "lo": lo, "hi": hi}
                     for p in (7, 14, 21, 42) for lo, hi in ((30, 70), (20, 80))],
}


# ------------------------------------------------------------- evaluation
def profit_factor(rets: np.ndarray) -> float:
    rets = np.asarray(rets)[np.isfinite(rets)]
    pos = rets[rets > 0].sum()
    neg = -rets[rets < 0].sum()
    return float(pos / neg) if neg > 1e-12 else 999.0


def next_bar_returns(ohlc: pd.DataFrame) -> np.ndarray:
    """Return earned by a position held after each bar (no lookahead)."""
    return np.log(ohlc["close"]).diff().shift(-1).to_numpy()


def backtest(ohlc: pd.DataFrame, pos: pd.Series, fee: float = FEE_TAKER) -> dict:
    """Fee-aware backtest of a position vector. `fee` is per side."""
    pos = pos.to_numpy(dtype=float)
    r = next_bar_returns(ohlc)
    cost = fee * np.abs(np.diff(np.concatenate([[0.0], pos])))
    rets = pos * r - cost
    rets = rets[np.isfinite(rets)]
    eq = np.cumprod(1 + rets)
    mdd = float(((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min())
    sd = rets.std()
    flips = int((np.abs(np.diff(np.concatenate([[0], pos])) > 0).sum()))
    return {
        "total_return": float(eq[-1] - 1),
        "sharpe": float(rets.mean() / sd * np.sqrt(HOURS_PER_YEAR)) if sd > 0 else 0.0,
        "max_drawdown": mdd,
        "profit_factor": profit_factor(rets),
        "trades": flips,
        "trades_per_day": flips / (len(rets) / 24),
    }


def optimize(strategy: str, ohlc: pd.DataFrame, fee: float = FEE_TAKER) -> dict:
    """Grid search a strategy on `ohlc` (use the last 4y before 'now').

    Returns {"params": ..., "stats": ...}. Objective = net profit factor,
    tie-break on fewer trades (less fee exposure).
    """
    fn = SIGNALS[strategy]
    r = next_bar_returns(ohlc)
    best, best_key = None, (-np.inf, 0.0)
    for p in GRIDS[strategy]:
        sig = fn(ohlc, **p).to_numpy(dtype=float)
        cost = fee * np.abs(np.diff(np.concatenate([[0.0], sig])))
        rets = sig * r - cost
        rets = rets[np.isfinite(rets)]
        key = (profit_factor(rets),
               -float((np.abs(np.diff(np.concatenate([[0], sig])) > 0)).sum()))
        if key > best_key:
            best, best_key = p, key
    sig = fn(ohlc, **best)
    return {"params": best, "stats": backtest(ohlc, sig, fee)}


def walkforward_params(strategy: str, ohlc: pd.DataFrame,
                       train_window: int = WF_TRAIN_WINDOW,
                       reopt_step: int = WF_REOPT_STEP) -> dict:
    """Re-run the validated schedule on the bot's own data: on each re-opt
    date, grid-search the most recent `train_window` bars. Returns the
    parameter history (log it — the judges check trace history)."""
    n = len(ohlc)
    history = []
    for t in range(train_window, n, reopt_step):
        params = optimize(strategy, ohlc.iloc[max(0, t - train_window):t])["params"]
        history.append({"bar": int(t), "time": str(ohlc.index[t]), "params": params})
    return {"history": history, "current": history[-1]["params"] if history else DEFAULT_PARAMS[strategy]}


if __name__ == "__main__":
    # quick self-test on real data
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    data = os.path.join(here, "..", "mcpt_screen", "data", "BTCUSD_3600.csv")
    df = pd.read_csv(data, index_col=0, parse_dates=True)
    df = df[df["close"] > 0]
    print("BTCUSD 1h, last 3 years, full size, 0.1%/side taker (long/flat set):\n")
    print(f"{'strategy':<18}{'params':<34}{'total':>8}{'Sharpe':>8}{'MDD%':>8}{'tr/d':>7}")
    tail = df.iloc[-3 * 24 * 365:]
    for name in SIGNALS:
        st = optimize(name, tail)
        p, s = st["params"], st["stats"]
        print(f"{name:<18}{str(p):<34}{s['total_return']*100:>7.1f}%{s['sharpe']:>8.2f}"
              f"{s['max_drawdown']*100:>7.1f}%{s['trades_per_day']:>7.2f}")
