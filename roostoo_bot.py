"""
roostoo_bot.py — the main loop. Run under systemd (roostoo-bot.service).

Each poll (default 60s — one /v3/ticker call covers ALL pairs):
  1. fold LastPrice into the open candle bucket (persisted every poll)
  2. on bucket close: compute the 5 signals, committee vote, enter/exit
  3. sweep expired maker orders (cancel -> market)
  4. hourly bookkeeping: daily-stop overlay, UTC day rollover

Crash safety: all state lives in SQLite (candles/orders/decisions/kv) + the
API itself (balance, pending orders). A restart resumes the open candle,
adopts pending orders, and continues — no manual intervention needed, and
per the rules, none is ever performed.
"""
from __future__ import annotations

import logging
import os
import signal as os_signal
import sys
import time

import config as cfg
from candles import CandleStore
from roostoo_client import RoostooClient, RoostooAPIError
from roostoo_signals import SIGNALS, SIGNALS_SHORT
from trader import Trader

log = logging.getLogger("bot")
_running = True


def _stop(signum, frame):
    global _running
    log.info("signal %s received — shutting down after this poll", signum)
    _running = False


class Bot:
    def __init__(self):
        self.store = CandleStore(cfg.DB_PATH)
        self.client = RoostooClient()
        self.trader = Trader(self.client, self.store)
        self.bucket_len = cfg.CANDLE_MINUTES * 60
        self.state: dict[str, dict] = {}   # pair -> open candle
        self.last_hour_book = 0
        self._resume_open_candles()

    # ------------------------------------------------------------ startup
    def _resume_open_candles(self) -> None:
        bucket = self._bucket_now()
        for pair in cfg.PAIRS:
            prev = self.store.get_candle(pair, bucket)
            if prev:
                self.state[pair] = {"bucket": bucket, **prev}
                log.info("resumed open candle %s bucket=%s n=%s (crash recovery)",
                         pair, bucket, prev["n"])
            else:
                self.state[pair] = None
        for pair in cfg.PAIRS:
            log.info("warm history %s: %d candles stored", pair, self.store.count(pair))

    def _bucket_now(self) -> int:
        return (self.client.now_ms() // 1000) // self.bucket_len * self.bucket_len

    # ------------------------------------------------------------ telegram
    def _tg(self, text: str) -> None:
        if not cfg.TG_NOTIFY_ON_TRADE:
            return
        try:
            from monitor import send_telegram
            send_telegram(text)
        except Exception as e:
            log.warning("telegram notify failed: %s", e)

    # ------------------------------------------------------------- signals
    def evaluate(self, pair: str, closed_bucket: int, tick: dict) -> None:
        """Committee vote on a freshly closed candle -> target position."""
        df = self.store.load(pair, cfg.WARMUP_HOURS, closed_bucket)
        need = 120
        if len(df) < need:
            log.info("%s warming up: %d/%d candles", pair, len(df), need)
            return
        signals, long_votes = {}, 0
        for name, params in cfg.STRATEGIES:
            table = SIGNALS_SHORT if name in SIGNALS_SHORT else SIGNALS
            if name not in table:
                log.warning("unknown strategy %s — skipped", name)
                continue
            v = float(table[name](df, **params).iloc[-1])
            signals[name] = v
            long_votes += 1 if v >= 1 else 0
        n_strat = len(signals) or 1

        equity = self.trader.equity(tick)
        if self._halted():
            target = 0
        elif cfg.SHORTS_ENABLED and long_votes == 0 and \
                sum(1 for v in signals.values() if v <= -1) >= cfg.MIN_AGREEMENT:
            target = -1
        else:
            target = 1 if long_votes >= cfg.MIN_AGREEMENT else 0

        trade = None
        if target == 1:
            held = self.trader.held(pair)
            if held <= 0:
                sleeve = equity * cfg.PER_PAIR_PCT
                deployed = self.trader.deployed_usd(tick)
                if deployed + sleeve > equity * cfg.MAX_TOTAL_EXPOSURE_PCT:
                    log.info("%s entry skipped: exposure cap", pair)
                else:
                    trade = self.trader.buy(pair, sleeve, tick,
                                            note=f"agree {long_votes}/{n_strat}")
                    if trade:
                        log.info("ENTER LONG %s qty=%g @~%g (%s)",
                                 pair, trade["qty"], trade["price"], trade["note"])
        elif target == 0:
            held = self.trader.held(pair)
            if held > 0:
                trade = self.trader.sell_all(pair, tick, note="signal flip")
                if trade:
                    log.info("EXIT LONG %s qty=%g @~%g", pair, trade["qty"], trade["price"])
            if cfg.SHORTS_ENABLED:
                s = self.trader.short_exit(pair, tick, note="signal flip")
                if s:
                    log.info("EXIT SHORT %s", pair)
        elif target == -1:
            s = self.trader.short_enter(pair, equity * cfg.PER_PAIR_PCT, tick,
                                        note=f"agree {sum(1 for v in signals.values() if v <= -1)}/{n_strat}")
            if s:
                trade = s
                log.info("ENTER SHORT %s @~%g", pair, s["price"])

        self.store.log_decision(pair, target, long_votes, equity, signals)
        sig_str = " ".join(f"{k}={v:.0f}" for k, v in signals.items())
        log.info("DECISION %s target=%d votes=%d/%d eq=$%.2f | %s",
                 pair, target, long_votes, n_strat, equity, sig_str)
        if trade and cfg.TG_NOTIFY_ON_TRADE:
            emoji = {"BUY": "🟢", "SELL": "🔴", "SHORT_OPEN": "🟣", "SHORT_CLOSE": "🟣"}
            self._tg(f"{emoji.get(trade['side'], '⚡')} {trade['side']} {trade['pair']} "
                     f"qty={trade['qty']:g} @~{trade['price']:g} "
                     f"({trade['status']}{', DRY' if trade['dry'] else ''}) — {trade['note']}")

    # -------------------------------------------------------- risk overlay
    def _halted(self) -> bool:
        return bool(self.store.kv_get("halted", False))

    def hourly_book(self, tick: dict) -> None:
        """UTC day rollover + daily-loss stop. Runs once per closed bucket."""
        now_s = self.client.now_ms() // 1000
        day = now_s // 86400
        equity = self.trader.equity(tick)

        if self.store.kv_get("day") != day:
            self.store.kv_set("day", day)
            self.store.kv_set("day_start_equity", equity)
            self.store.kv_set("halted", False)
            log.info("new UTC day %d — day-start equity $%.2f", day, equity)

        day_start = float(self.store.kv_get("day_start_equity", equity) or equity)
        if not self._halted() and equity < day_start * (1 - cfg.DAILY_LOSS_LIMIT_PCT):
            self.store.kv_set("halted", True)
            log.warning("DAILY STOP: equity $%.2f < $%.2f (start $%.2f) — flattening",
                        equity, day_start * (1 - cfg.DAILY_LOSS_LIMIT_PCT), day_start)
            if not self.trader.dry:
                try:
                    self.client.cancel_order()  # cancel ALL pending orders
                except RoostooAPIError as e:
                    log.warning("cancel-all: %s", e)
            for pair in cfg.PAIRS:
                if pair in tick:
                    self.trader.sell_all(pair, tick, note="daily stop")
                    self.trader.short_exit(pair, tick, note="daily stop")
            self._tg(f"🛑 DAILY STOP — equity ${equity:,.2f} (day start ${day_start:,.2f}). "
                     f"Flattened, flat until next UTC day.")

    # --------------------------------------------------------------- poll
    def poll(self) -> None:
        tick = (self.client.ticker().get("Data") or {})
        bucket = self._bucket_now()

        for pair in cfg.PAIRS:
            t = tick.get(pair)
            px = float(t["LastPrice"]) if t else None
            if px is None:
                log.warning("no ticker for %s this poll", pair)
                continue
            st = self.state.get(pair)
            if st is None or st["bucket"] != bucket:
                if st is not None:
                    # previous bucket closed: persist it (already upserted every
                    # poll), then evaluate on the closed candle
                    self._after_close(pair, st, tick)
                st = self.state[pair] = {
                    "bucket": bucket, "o": px, "h": px, "l": px, "c": px, "n": 1}
            else:
                st["h"] = max(st["h"], px)
                st["l"] = min(st["l"], px)
                st["c"] = px
                st["n"] += 1
            self.store.upsert_candle(pair, st["bucket"], st["o"], st["h"],
                                     st["l"], st["c"], st["n"])

        if not self.trader.dry:
            self.trader.sweep_makers(tick)

        if bucket != self.last_hour_book:
            self.last_hour_book = bucket
            try:
                self.hourly_book(tick)
            except Exception as e:
                log.error("hourly bookkeeping failed: %s", e)

    def _after_close(self, pair: str, closed: dict, tick: dict) -> None:
        log.info("candle close %s bucket=%s o=%.6g h=%.6g l=%.6g c=%.6g (n=%s)",
                 pair, closed["bucket"], closed["o"], closed["h"],
                 closed["l"], closed["c"], closed["n"])
        try:
            self.evaluate(pair, closed["bucket"], tick)
        except Exception as e:
            log.exception("evaluate failed for %s: %s", pair, e)

    # --------------------------------------------------------------- main
    def run(self) -> None:
        os_signal.signal(os_signal.SIGTERM, _stop)
        os_signal.signal(os_signal.SIGINT, _stop)
        log.info("=" * 64)
        log.info("ROOSTOO BOT START | dry_run=%s | pairs=%s | poll=%ss | candle=%sm",
                 self.trader.dry, cfg.PAIRS, cfg.POLL_SECONDS, cfg.CANDLE_MINUTES)
        log.info("strategies: %s (min agreement %d, shorts %s)",
                 [n for n, _ in cfg.STRATEGIES], cfg.MIN_AGREEMENT, cfg.SHORTS_ENABLED)
        log.info("=" * 64)
        while _running:
            t0 = time.time()
            try:
                self.poll()
            except RoostooAPIError as e:
                log.error("API error (will retry next poll): %s", e)
                self.store.log_api("poll", False, str(e))
            except Exception as e:
                log.exception("poll error (will retry): %s", e)
            time.sleep(max(1.0, cfg.POLL_SECONDS - (time.time() - t0)))
        log.info("roostoo bot stopped cleanly")


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(cfg.LOG_FILE)
    fh.setFormatter(fmt)
    root.addHandler(fh)


def main() -> None:
    _setup_logging()
    if cfg.DRY_RUN:
        log.warning("*** DRY RUN MODE — orders are simulated, nothing is sent ***")
    Bot().run()


if __name__ == "__main__":
    main()
