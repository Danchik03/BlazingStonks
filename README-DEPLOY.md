# Roostoo Hackathon — Deployment Guide (AWS)

Everything in `bot/` is the entire system:

```
bot/
├── config.py            # ALL the variables (pairs, strategies, sizing, risk, timing)
├── roostoo_client.py    # API client: HMAC-SHA256 signing, clock sync, 30/min guard
├── roostoo_signals.py   # the 5 MCPT-validated strategies + validation code
├── candles.py           # ticker → 1h candles, SQLite persistence (crash-safe)
├── trader.py            # maker-first execution, precision, dry-run
├── roostoo_bot.py       # main loop (systemd: roostoo-bot.service)
├── monitor.py           # Telegram account snapshot (systemd timer, 10 min)
├── fetch_history.py     # warm-start candles from data.binance.vision
├── roostoo-bot.service / roostoo-monitor.{service,timer}
└── .env                 # keys (GITIGNORED — never committed, never in code)
```

**Flow:** every 60 s one `/v3/ticker` call (all pairs) is folded into local 1h
candles. On each candle close the 5 strategies vote; ≥ `MIN_AGREEMENT` longs →
enter (maker limit, market after 10 min); any flip → exit (always market).
Hourly: daily-loss overlay (−3% → flatten, flat until next UTC day).
Everything is logged to SQLite (`bot.db`) + `bot.log` + Telegram.
Rate budget: ~1 call/min polling + ~4 calls/hour bookkeeping ≪ 30/min.

---

## Part 1 — Get the code onto a GitHub repo (one time)

The repo must exist and be committed **before Oct 9** (judges review the
commit history). Locally:

```bash
cd roostoo/bot            # this directory (already a clean set of files)
git init && git add -A
git commit -m "Bot v1: 5 MCPT-validated LF strategies, maker-first execution, daily stop"
# push to a GitHub repo (public is fine — NO secrets are in the repo; .env is gitignored)
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Double-check before pushing: `git status` shows nothing sensitive, and
`git grep -i "RST_API\|SECRET\|TG_BOT"` finds nothing.

## Part 2 — Launch the EC2 instance (per the official AWS guide)

1. Open the portal: `https://d-906625dad1.awsapps.com/start`
   → enter your email → enter the verification code → MFA → **Management Console**.
2. Region is **locked to ap-southeast-2 (Sydney)** — the template handles it.
3. EC2 → **Instances → Launch Templates → `Hackathon-Starter-Template` → Launch**.
   t3.medium, 30 GB EBS are pre-set. Do **not** attach a key pair
   (SSH is blocked anyway). ⚠️ **Exactly one instance** — a second launch is
   auto-terminated.
4. Wait for the instance to be *running*, then: instance → **Connect →
   Session Manager → Connect** (browser terminal. This is the ONLY way in.)

## Part 3 — Install + fetch (in the Session Manager terminal)

```bash
sudo dnf install -y python3 python3-pip git tmux
python3 -m pip install numpy pandas
git clone https://github.com/<you>/<repo>.git ~/roostoo
cd ~/roostoo/bot
```

## Part 4 — Credentials (`.env`)

Paste your keys (test key now; swap in the **competition key** when the main
round runs — same file, restart the service):

```bash
cd ~/roostoo/bot
cat > .env <<'EOF'
RST_API_KEY=<your key>
RST_API_SECRET=<your secret>
TG_BOT_TOKEN=<telegram bot token>
TG_CHAT_ID=<your chat id>
EOF
chmod 600 .env
```

Verify the key works:

```bash
python3 roostoo_client.py     # serverTime, pairs, ticker, balance, shorts probe
python3 monitor.py            # prints a report + sends the first Telegram ping
```

## Part 5 — Warm-start history

```bash
python3 fetch_history.py
```

Downloads ~10 days of 1h candles per pair from `data.binance.vision`
(free, no auth — the official recommended source; Roostoo prices stream from
Binance). ~30 s total.

## Part 6 — Dry run first (always)

```bash
DRY_RUN=true POLL_SECONDS=5 CANDLE_MINUTES=1 python3 roostoo_bot.py
```

(The 1-minute candles + 5 s polls make the whole loop — candle close → vote →
order → log → Telegram — exercise within ~2 minutes, on a separate DB state;
watch it for a few buckets, Ctrl-C.) Production run is the normal command:

```bash
DRY_RUN=true python3 roostoo_bot.py    # real 1h candles, simulated orders
```

Leave this running through the hour boundary and confirm in Telegram that the
first simulated decision arrived.

## Part 7 — Go live

```bash
sudo cp roostoo-bot.service roostoo-monitor.service roostoo-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now roostoo-bot
sudo systemctl enable --now roostoo-monitor.timer
```

Check:

```bash
journalctl -u roostoo-bot -f          # live log
systemctl list-timers | grep roostoo  # monitor firing every 10 min
sqlite3 bot.db "SELECT * FROM orders ORDER BY id DESC LIMIT 5;"   # if sqlite3 is installed
```

Confirm on Telegram: the first **real** order appears as a bot decision
message, and `monitor.py` keeps snapshotting the account. **The first trade on
the competition key should happen as early as possible** (the Oct 1 8 pm
first-trade deadline is already past — don't let it slide further; full-period
activity is a finalist consideration).

## Part 8 — Operating notes

- **Never** place manual API calls or trade by hand during the round
  (strictly prohibited). All changes = edit `config.py` → commit →
  `sudo systemctl restart roostoo-bot`.
- Crashes self-heal: systemd restarts in 15 s; the bot resumes the open
  candle from SQLite and adopts any pending orders from the API.
- The daily stop is the only time the bot force-flattens (−3% intraday →
  flat until next UTC day, Telegram ping).
- **End of round:** `sudo systemctl stop roostoo-bot` — the system
  auto-liquidates at the close; do not try to manual-unwind.
- `bot.db` (SQLite) holds the full audit trail: candles, every order
  (dry + live), every decision with the signal snapshot, every API error.
  It *is* the evidence for the code review.
- If you want the long/short upgrade later: test `/v6` on the key first
  (shorts verified working on the test key 2026-10-03), then set
  `SHORTS_ENABLED=true` in `config.py` and swap `rsi_trend_lf` →
  `rsi_trend` in `STRATEGIES`. Commit with a clear message.
