"""
Telegram account monitor for the Roostoo competition bot.

Pulls a live snapshot (equity, holdings, pending orders, recent fills) and pushes
a compact status message to a Telegram chat via the Bot API.

Setup (one time):
  1. Talk to @BotFather on Telegram -> /newbot -> copy the token.
  2. Send any message to YOUR new bot, then open:
       https://api.telegram.org/bot<TOKEN>/getUpdates
     and read "chat":{"id":123456789} for your chat_id.
  3. Put both in bot/.env:
       TG_BOT_TOKEN=123456:ABC-...
       TG_CHAT_ID=123456789

Usage:
  python3 monitor.py            # print report (sends if TG creds present)
  python3 monitor.py --send     # force send
In production the main bot loop (or a systemd timer on the EC2) calls send_report().
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone

from roostoo_client import RoostooClient, RoostooAPIError, _load_env_file

LAST_SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_snapshot.json")


def _wallet(balance_payload: dict) -> dict:
    """SpotWallet + MarginWallet merged: {COIN: {Free, Lock, PendingOrders, ShortCollateral}}."""
    w: dict = {}
    for section in ("SpotWallet", "MarginWallet", "Wallet"):  # Wallet = legacy doc shape
        for coin, a in (balance_payload.get(section) or {}).items():
            w.setdefault(coin, {"Free": 0.0, "Lock": 0.0, "PendingOrders": 0, "ShortCollateral": 0.0})
            for k in w[coin]:
                w[coin][k] += float(a.get(k) or 0)
    return w


def build_report(client: RoostooClient) -> str:
    lines = [f"Roostoo bot — {datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M %Z}"]
    errors = []

    # --- equity + holdings ---
    try:
        bal = client.balance()
        tick = client.ticker().get("Data") or {}
        w = _wallet(bal)
        usd_free = float(w.get("USD", {}).get("Free", 0.0))
        holdings = []
        coin_value = 0.0
        for coin, a in w.items():
            if coin == "USD":
                continue
            amt = a["Free"] + a["Lock"]
            px = (tick.get(f"{coin}/USD") or {}).get("LastPrice")
            val = amt * px if px else 0.0
            coin_value += val
            if amt > 0:
                holdings.append(f"{coin} {amt:g} ≈ ${val:,.2f}")
        equity = usd_free + coin_value
        try:
            prev = json.load(open(LAST_SNAPSHOT))
            if prev.get("equity"):
                chg = (equity / prev["equity"] - 1) * 100
                lines.append(f"Equity: ${equity:,.2f}  ({chg:+.3f}% vs last snapshot)")
            else:
                lines.append(f"Equity: ${equity:,.2f}")
        except Exception:
            lines.append(f"Equity: ${equity:,.2f}")
        json.dump({"equity": equity, "ts": time.time()}, open(LAST_SNAPSHOT, "w"))
        if holdings:
            lines.append("Holdings: " + " · ".join(holdings))
        else:
            lines.append(f"Holdings: flat (${usd_free:,.2f} cash)")
        shorts = {c: a["ShortCollateral"] for c, a in w.items() if a["ShortCollateral"] > 0}
        lines.append("Shorts: " + (" · ".join(f"{c} ${v:,.2f} coll" for c, v in shorts.items())
                                   if shorts else "none"))
    except Exception as e:
        errors.append(f"balance: {e}")

    # --- pending orders ---
    try:
        po = client.query_order(pending_only=True, limit=20)
        orders = po.get("OrderMatched") or []
        if orders:
            lines.append(f"Pending orders ({len(orders)}):")
            for o in orders[:8]:
                lines.append(f"  {o['Side']:4s} {o['Quantity']:g} {o['Pair']} @ {o.get('Price', '-')} "
                             f"({o['Type']}, id {o['OrderID']})")
            if len(orders) > 8:
                lines.append(f"  … +{len(orders) - 8} more")
        else:
            lines.append("Pending orders: none")
    except RoostooAPIError:
        lines.append("Pending orders: none")  # 'no order matched' is the empty state
    except Exception as e:
        errors.append(f"pending: {e}")

    # --- recent fills ---
    try:
        ro = client.query_order(limit=5)
        recent = [o for o in (ro.get("OrderMatched") or []) if o.get("Status") == "FILLED"][:5]
        if recent:
            lines.append("Recent fills:")
            for o in recent:
                fee = o.get("CommissionChargeValue", 0)
                lines.append(f"  {o['Side']:4s} {o.get('FilledQuantity', o.get('Quantity', 0)):g} "
                             f"{o['Pair']} @ {o.get('FilledAverPrice', o.get('Price', '-'))} "
                             f"{o.get('Role', '?')} fee {fee:g}")
    except Exception as e:
        errors.append(f"fills: {e}")

    if errors:
        lines.append("ERRORS: " + " | ".join(errors))
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    _load_env_file()
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN / TG_CHAT_ID not set in bot/.env — report printed, not sent.")
        return
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read().decode())
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram API error: {resp}")


def send_report() -> str:
    """One full monitor cycle. Returns the report text."""
    client = RoostooClient()
    text = build_report(client)
    send_telegram(text)
    return text


if __name__ == "__main__":
    print(send_report())
