"""
Roostoo mock-exchange API client (competition partner API).

Spec: https://github.com/roostoo/Roostoo-API-Documents
  Base URL   : https://mock-api.roostoo.com
  Auth levels:
    RCL_NoVerification -> public (serverTime, exchangeInfo)
    RCL_TSCheck        -> 13-digit ms `timestamp` param, no signature (ticker)
    RCL_TopLevelCheck  -> timestamp + RST-API-KEY + MSG-SIGNATURE headers (everything else)
  Signature  : HMAC-SHA256(secretKey, totalParams) as lowercase hex, where totalParams is the
               params sorted by key, joined as k=v with &  (query string for GET-style bodies;
               all order/query endpoints here are POST with x-www-form-urlencoded body).
  Failures   : HTTP 200 with {"Success": false, "ErrMsg": "..."}
  Budget     : 30 API calls/minute INCLUDING queries — guarded locally below.

Credentials come from env vars RST_API_KEY / RST_API_SECRET or a local .env file next to this
module. .env is gitignored — never commit credentials (repo commit history is reviewed).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("RST_BASE_URL", "https://mock-api.roostoo.com")


def _load_env_file() -> None:
    env = Path(__file__).resolve().with_name(".env")
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


class RoostooAPIError(RuntimeError):
    def __init__(self, msg: str, payload=None):
        super().__init__(msg)
        self.payload = payload


class RoostooClient:
    def __init__(self, api_key: str | None = None, api_secret: str | None = None,
                 base_url: str = BASE_URL, timeout: float = 15.0, enforce_budget: bool = True):
        _load_env_file()
        self.api_key = api_key or os.environ.get("RST_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("RST_API_SECRET", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.enforce_budget = enforce_budget
        self._clock_offset_ms = 0  # server time - local time
        self._call_times: list[float] = []

    # ---------------- internals ----------------

    def _bump_budget(self) -> None:
        now = time.time()
        self._call_times = [t for t in self._call_times if now - t < 60.0]
        if self.enforce_budget and len(self._call_times) >= 30:
            raise RoostooAPIError("local rate-limit guard: already 30 calls this minute")
        self._call_times.append(now)

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._clock_offset_ms

    @staticmethod
    def _total_params(params: dict) -> str:
        return "&".join(f"{k}={params[k]}" for k in sorted(params))

    def _sync_clock(self, payload: dict) -> None:
        st = payload.get("ServerTime")
        if isinstance(st, (int, float)):
            self._clock_offset_ms = int(st) - int(time.time() * 1000)

    def _request(self, path: str, params: dict, *, signed: bool, needs_ts: bool,
                 method: str = "POST") -> dict:
        self._bump_budget()
        params = {k: (str(v) if not isinstance(v, str) else v) for k, v in params.items()}
        if needs_ts or signed:
            params["timestamp"] = str(self._now_ms())

        headers = {"User-Agent": "roostoo-bot/0.1"}
        body = None
        url = self.base_url + path
        if signed:
            if not self.api_key or not self.api_secret:
                raise RoostooAPIError("RST_API_KEY / RST_API_SECRET not set (set bot/.env)")
            headers["RST-API-KEY"] = self.api_key
            headers["MSG-SIGNATURE"] = hmac.new(
                self.api_secret.encode(), self._total_params(params).encode(),
                hashlib.sha256,
            ).hexdigest()
        if method == "GET":
            if params:
                url += "?" + self._total_params(params)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = self._total_params(params).encode()

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise RoostooAPIError(f"HTTP {e.code} on {path}") from e
        except Exception as e:  # DNS/TLS/timeout
            raise RoostooAPIError(f"network error on {path}: {e}") from e

        self._sync_clock(payload)
        if isinstance(payload, dict) and payload.get("Success") is False:
            raise RoostooAPIError(f"{path}: {payload.get('ErrMsg')}", payload)
        return payload

    def now_ms(self) -> int:
        """Server-aligned wall clock (ms). Use this — not the local clock — for
        hour-bucket boundaries so candle closes follow the exchange."""
        return self._now_ms()

    # ---------------- public (no auth) ----------------

    def server_time(self) -> dict:
        return self._request("/v3/serverTime", {}, signed=False, needs_ts=False, method="GET")

    def exchange_info(self) -> dict:
        """Pair list + precision + MiniOrder + IsRunning."""
        return self._request("/v3/exchangeInfo", {}, signed=False, needs_ts=False, method="GET")

    # ---------------- timestamp only (RCL_TSCheck) ----------------

    def ticker(self, pair: str | None = None) -> dict:
        """All pairs' MaxBid/MinAsk/LastPrice/24h Change (pair optional). One call = all pairs."""
        return self._request("/v3/ticker", {"pair": pair} if pair else {},
                             signed=False, needs_ts=True, method="GET")

    # ---------------- signed (RCL_TopLevelCheck) ----------------

    def balance(self) -> dict:
        """Live shape: {SpotWallet, MarginWallet} = {COIN: {Free, Lock, PendingOrders, ShortCollateral}}.
        (Old docs said 'Wallet'; mock now returns SpotWallet/MarginWallet.)"""
        return self._request("/v3/balance", {}, signed=True, needs_ts=False, method="GET")

    def pending_count(self) -> dict:
        """TotalPending + OrderPairs. Raises with ErrMsg 'no pending order...' when zero."""
        return self._request("/v3/pending_count", {}, signed=True, needs_ts=False, method="GET")

    def place_order(self, pair: str, side: str, quantity, order_type: str = "MARKET",
                    price=None) -> dict:
        """side in {BUY, SELL}; quantity must respect AmountPrecision, price PricePrecision.
        MARKET fills immediately (taker); LIMIT needs price (maker if not immediately filled)."""
        p = {"pair": pair, "side": side, "type": order_type, "quantity": quantity}
        if price is not None:
            p["price"] = price
        if order_type == "LIMIT" and price is None:
            raise ValueError("LIMIT orders require price")
        return self._request("/v3/place_order", p, signed=True, needs_ts=False)

    def query_order(self, order_id=None, pair: str | None = None, pending_only: bool | None = None,
                    limit: int | None = None, offset: int | None = None) -> dict:
        """order_id alone, or pair/offset/limit/pending_only filters (order_id + others = error)."""
        p: dict = {}
        if order_id is not None:
            p["order_id"] = order_id
        else:
            if pair is not None:
                p["pair"] = pair
            if pending_only is not None:
                p["pending_only"] = "TRUE" if pending_only else "FALSE"
            if limit is not None:
                p["limit"] = int(limit)
            if offset is not None:
                p["offset"] = int(offset)
        return self._request("/v3/query_order", p, signed=True, needs_ts=False)

    def cancel_order(self, order_id=None, pair: str | None = None) -> dict:
        """None+None cancels ALL pending orders. Only pending orders can be canceled."""
        p: dict = {}
        if order_id is not None:
            p["order_id"] = order_id
        elif pair is not None:
            p["pair"] = pair
        return self._request("/v3/cancel_order", p, signed=True, needs_ts=False)

    # ---------------- v6 shorting (same signing, same key) ----------------
    # May be disabled per competition ("this competition does not allow short positions").

    def short_open(self, pair: str, collateral, order_type: str | None = None,
                   price=None) -> dict:
        """collateral = USD locked (min 1); qty = collateral/Entry floored to AmountPrecision.
        Market fills at MaxBid. LIMIT stays pending. 0.1% open fee charged at ACCEPTANCE."""
        p = {"pair": pair, "collateral": collateral}
        if order_type:
            p["order_type"] = order_type
        if price is not None:
            p["price"] = price
        return self._request("/v6/short_open", p, signed=True, needs_ts=False)

    def short_close(self, pair: str, close_qty=None, close_pct=None) -> dict:
        """Always market at MinAsk, 0.1% fee, reduce-only. PnL capped at collateral."""
        p = {"pair": pair}
        if close_qty is not None:
            p["close_qty"] = close_qty
        if close_pct is not None:
            p["close_pct"] = close_pct
        return self._request("/v6/short_close", p, signed=True, needs_ts=False)

    def short_positions(self) -> dict:
        """NOTE: 404 on current mock build (Sep 2026) — track shorts from short_open/close
        responses and balance ShortCollateral instead. Retained in case the endpoint ships."""
        return self._request("/v6/short_positions", {}, signed=True, needs_ts=False)


def _selftest() -> None:
    c = RoostooClient()
    print("serverTime :", c.server_time())
    info = c.exchange_info()
    print("IsRunning  :", info.get("IsRunning"),
          "| InitialWallet:", info.get("InitialWallet"),
          "| pairs:", sorted(info.get("TradePairs", {})))
    t = c.ticker()
    print("ticker     :")
    for k, v in (t.get("Data") or {}).items():
        print(f"  {k:10s} bid={v.get('MaxBid')} ask={v.get('MinAsk')} last={v.get('LastPrice')} 24h={v.get('Change')}")
    b = c.balance()
    print("balance    :", json.dumps({k: b.get(k) for k in ("SpotWallet", "MarginWallet") if b.get(k)}))
    try:
        p = c.pending_count()
        print("pending    :", p)
    except RoostooAPIError as e:
        print("pending    : none (expected when flat)", str(e))
    try:
        s = c.short_positions()
        print("shorts     :", s)
    except RoostooAPIError as e:
        print("shorts     : UNAVAILABLE ->", e)


if __name__ == "__main__":
    _selftest()
