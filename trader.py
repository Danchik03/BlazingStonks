"""
trader.py — turn target positions into Roostoo orders, maker-first.

Rules:
  * The API is the source of truth for money (balance, pending orders);
    SQLite is the audit trail (every order, dry or live, is logged).
  * Precision is enforced from /v3/exchangeInfo per pair
    (AmountPrecision floors qty, PricePrecision rounds price, MiniOrder
    raises tiny orders to the minimum).
  * Entries: LIMIT at the maker side (buy at MaxBid) for MAKER_WAIT_MINUTES,
    then cancel + MARKET. Exits: always MARKET (risk outranks the fee).
  * DRY_RUN: identical logic, but orders are simulated at the spread and
    marked DRY_RUN in the orders table — nothing is sent to the exchange.
"""
from __future__ import annotations

import logging
import os
import time

from roostoo_client import RoostooAPIError
import config as cfg


class Trader:
    def __init__(self, client, store):
        self.c = client
        self.store = store
        self.log = logging.getLogger("trader")
        info = client.exchange_info()
        if info.get("IsRunning") is False:
            raise RuntimeError(f"exchange not running: {info}")
        self.meta = {p: m for p, m in (info.get("TradePairs") or {}).items()
                     if m.get("CanTrade")}
        missing = [p for p in cfg.PAIRS if p not in self.meta]
        if missing:
            raise RuntimeError(f"pairs not tradable on exchange: {missing}; "
                               f"listed: {sorted(self.meta)}")
        self.dry = cfg.DRY_RUN
        self.log.info("exchange OK | %d pairs tradable | dry_run=%s | pairs=%s",
                      len(self.meta), self.dry, cfg.PAIRS)

    # ------------------------------------------------------------ precision
    def _q(self, pair: str, value: float) -> float:
        """Floor to the pair's AmountPrecision."""
        f = 10 ** self.meta[pair]["AmountPrecision"]
        return int(value * f) / f

    def _p(self, pair: str, value: float) -> float:
        """Round to the pair's PricePrecision."""
        f = 10 ** self.meta[pair]["PricePrecision"]
        return round(value * f) / f

    def _qty_to_min_order(self, pair: str, price: float) -> float:
        min_px = self.meta[pair]["MiniOrder"] / price
        return self._q(pair, max(min_px, 0))

    # ---------------------------------------------------------- market data
    def equity(self, tick: dict) -> float:
        """Total equity in USD = cash (free+locked) + coin holdings x LastPrice.
        Costs one /v3/balance call."""
        bal = self.c.balance()
        usd, coins = 0.0, 0.0
        for section in ("SpotWallet", "MarginWallet"):
            for coin, a in (bal.get(section) or {}).items():
                free = float(a.get("Free", 0) or 0)
                lock = float(a.get("Lock", 0) or 0)
                if coin == "USD":
                    usd += free + lock
                else:
                    px = (tick.get(f"{coin}/USD") or {}).get("LastPrice")
                    coins += (free + lock) * (px or 0.0)
        return usd + coins

    def held(self, pair: str) -> float:
        """Coin qty delivered and free to sell right now (Free, not Lock).
        Costs one /v3/balance call."""
        coin = pair.split("/")[0]
        bal = self.c.balance()
        total = 0.0
        for section in ("SpotWallet", "MarginWallet"):
            a = (bal.get(section) or {}).get(coin)
            if a:
                total += float(a.get("Free", 0) or 0)
        return self._q(pair, total)

    def deployed_usd(self, tick: dict) -> float:
        """Total value of coin holdings across all pairs."""
        bal = self.c.balance()
        val = 0.0
        for section in ("SpotWallet", "MarginWallet"):
            for coin, a in (bal.get(section) or {}).items():
                if coin == "USD":
                    continue
                px = (tick.get(f"{coin}/USD") or {}).get("LastPrice")
                if px:
                    val += (float(a.get("Free", 0) or 0) + float(a.get("Lock", 0) or 0)) * px
        return val

    # -------------------------------------------------------------- orders
    def _record(self, pair, side, otype, qty, price, status, role, fee,
                api_order_id, note) -> dict:
        self.store.log_order(pair, side, otype, qty, price, status, role, fee,
                             api_order_id, note)
        return {"pair": pair, "side": side, "type": otype, "qty": qty,
                "price": price, "status": status, "role": role, "fee": fee,
                "api_order_id": api_order_id, "note": note, "dry": self.dry}

    def _dry_fill(self, pair, side, otype, qty, price, note) -> dict:
        self.log.info("[DRY_RUN] %s %s qty=%g price=%g note=%s", side, pair, qty, price, note)
        return self._record(pair, side, otype, qty, price, "DRY_FILLED", "DRY",
                            qty * price * (cfg.FEE_MAKER if otype == "LIMIT" else cfg.FEE_TAKER),
                            "-", f"DRY_RUN {note}".strip())

    def buy(self, pair: str, usd_amount: float, tick: dict, note: str = "") -> dict | None:
        """Enter long. Maker limit at MaxBid (fills when price ticks down),
        market fallback handled by sweep_makers()."""
        t = tick[pair]
        bid, ask = float(t["MaxBid"]), float(t["MinAsk"])
        q = self._q(pair, usd_amount / bid)
        if q <= 0:
            q = self._qty_to_min_order(pair, bid)
        if q * bid < self.meta[pair]["MiniOrder"]:
            self.log.warning("skip entry %s: qty rounds below MiniOrder", pair)
            return None
        if self.dry:
            return self._dry_fill(pair, "BUY", "LIMIT", q, self._p(pair, bid), note)
        if cfg.MAKER_FIRST:
            r = self.c.place_order(pair, "BUY", q, "LIMIT", self._p(pair, bid))
        else:
            r = self.c.place_order(pair, "BUY", q, "MARKET")
        od = r.get("OrderDetail") or {}
        return self._record(pair, "BUY", od.get("Type", "?"), float(od.get("Quantity", q)),
                            float(od.get("Price", bid) or bid), od.get("Status", "?"),
                            od.get("Role", "?"), float(od.get("CommissionChargeValue", 0) or 0),
                            str(od.get("OrderID", "")), note)

    def sell_all(self, pair: str, tick: dict, note: str = "") -> dict | None:
        """Exit long: market (always) for the free balance."""
        t = tick[pair]
        ask = float(t["MinAsk"])
        q = self.held(pair)
        if q <= 0:
            return None
        if self.dry:
            return self._dry_fill(pair, "SELL", "MARKET", q, ask, note)
        if cfg.EXITS_MARKET:
            r = self.c.place_order(pair, "SELL", q, "MARKET")
        else:
            r = self.c.place_order(pair, "SELL", q, "LIMIT", self._p(pair, ask))
        od = r.get("OrderDetail") or {}
        return self._record(pair, "SELL", od.get("Type", "?"), float(od.get("Quantity", q)),
                            float(od.get("FilledAverPrice") or od.get("Price") or ask),
                            od.get("Status", "?"), od.get("Role", "?"),
                            float(od.get("CommissionChargeValue", 0) or 0),
                            str(od.get("OrderID", "")), note)

    def sweep_makers(self, tick: dict) -> list[dict]:
        """Cancel entry LIMIT orders older than MAKER_WAIT_MINUTES, retry as
        MARKET. Runs against API state, so it also adopts orders placed before
        a crash/restart. Dry-run has no API orders to sweep."""
        out = []
        if self.dry:
            return out
        try:
            pending = self.c.query_order(pending_only=True)
        except RoostooAPIError:
            return out  # 'no order matched'
        for od in pending.get("OrderMatched") or []:
            if od.get("Type") != "LIMIT" or od.get("Side") != "BUY":
                continue
            age_min = (self.c.now_ms() - float(od.get("CreateTimestamp", 0))) / 60000
            if age_min < cfg.MAKER_WAIT_MINUTES:
                continue
            pair, q = od["Pair"], float(od.get("Quantity", 0))
            if pair not in self.meta or q <= 0:
                continue
            try:
                self.c.cancel_order(order_id=od["OrderID"])
                self.log.info("maker expired after %.0f min -> market %s %g", age_min, pair, q)
                if pair in tick:
                    r = self.c.place_order(pair, "BUY", q, "MARKET")
                    nod = r.get("OrderDetail") or {}
                    out.append(self._record(
                        pair, "BUY", nod.get("Type", "MARKET"), q,
                        float(nod.get("FilledAverPrice") or 0), nod.get("Status", "?"),
                        nod.get("Role", "?"),
                        float(nod.get("CommissionChargeValue", 0) or 0),
                        str(nod.get("OrderID", "")), f"maker-expired {age_min:.0f}min"))
            except RoostooAPIError as e:
                self.log.error("sweep failed for order %s: %s", od.get("OrderID"), e)
        return out

    # ------------------------------------------------------------ shorting
    # /v6 verified working on the test key (2026-10-03). Default OFF.
    # NOTE: /v6/short_positions 404s on the current mock build, so short state
    # is tracked via kv (shorts) — updated on every enter/exit.
    def short_enter(self, pair: str, usd_collateral: float, tick: dict,
                    note: str = "") -> dict | None:
        if not cfg.SHORTS_ENABLED:
            return None
        t = tick[pair]
        bid = float(t["MaxBid"])
        if self.dry:
            return self._dry_fill(pair, "SHORT_OPEN", "MARKET", usd_collateral / bid, bid, note)
        r = self.c.short_open(pair, usd_collateral)  # market at MaxBid
        shorts = self.store.kv_get("shorts", {}) or {}
        shorts[pair] = {"qty": float(r.get("ShortQty", 0)),
                        "entry": float(r.get("EntryPrice", bid)),
                        "collateral": float(r.get("Collateral", usd_collateral)),
                        "opened_ts": int(time.time())}
        self.store.kv_set("shorts", shorts)
        return self._record(pair, "SHORT_OPEN", "MARKET", float(r.get("ShortQty", 0)),
                            float(r.get("EntryPrice", bid)), r.get("Status", "?"), "TAKER",
                            float(r.get("OpenFee", 0) or 0), str(r.get("ID", "")), note)

    def short_exit(self, pair: str, tick: dict, note: str = "") -> dict | None:
        shorts = self.store.kv_get("shorts", {}) or {}
        if pair not in shorts:
            return None
        pos = shorts[pair]
        if self.dry:
            self.store.kv_set("shorts", {k: v for k, v in shorts.items() if k != pair})
            return self._dry_fill(pair, "SHORT_CLOSE", "MARKET", pos["qty"],
                                  float(tick[pair]["MinAsk"]), note)
        r = self.c.short_close(pair, close_pct=100)  # always market
        self.store.kv_set("shorts", {k: v for k, v in shorts.items() if k != pair})
        return self._record(pair, "SHORT_CLOSE", "MARKET", float(r.get("ClosedQty", pos["qty"])),
                            float(r.get("ClosePrice", 0)), "FILLED" if r.get("FullyClosed") else "PARTIAL",
                            "TAKER", float(r.get("CloseFee", 0) or 0), "", note)


# fee constants live in config for the dry-fill cost estimate
cfg.FEE_TAKER = 0.001
cfg.FEE_MAKER = 0.0005
