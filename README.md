# US Representatives Copy-Trading Bot

Mirrors disclosed US Representative + Senator (and spouse) stock purchases on
Alpaca paper-trading. Every market day, pulls fresh Periodic Transaction
Reports (PTRs) from the public House and Senate Stock Watcher feeds, picks
tickers where multiple member-households disclosed buys, opens positions, and
manages them with layered take-profits, trailing stops, a drawdown kill
switch, and an adaptive entry-threshold learner.

> **STOCK Act caveat.** Members can file up to 45 days after a trade.
> "Today's filings" really means "trades executed 1–45 days ago." This is a
> *follow-the-flow* signal, not a frontrunning one.
>
> **Paper trading only.** Endpoints, sizing, and risk defaults are tuned for
> Alpaca paper. Do not point this at a live account without first auditing
> every constant.

---

## How it works

```
┌─────────────────────────────┐    ┌─────────────────────────────┐
│ House Stock Watcher (S3)    │    │ Senate Stock Watcher (S3)   │
│ + GitHub mirror fallback    │    │ + GitHub mirror fallback    │
└──────────────┬──────────────┘    └──────────────┬──────────────┘
               │                                  │
               └──────────────┬───────────────────┘
                              ▼
                  ┌─────────────────────────┐
                  │ congress.py             │
                  │  • normalize records    │
                  │  • filter Purchases     │
                  │    in last N days       │
                  │  • dedup by household   │
                  │    (member + spouse =   │
                  │    one buyer)           │
                  │  • aggregate per ticker │
                  └────────────┬────────────┘
                               ▼
                  ┌─────────────────────────┐
                  │ trader.py               │
                  │  • equity / risk gates  │
                  │  • enter top-ranked     │
                  │    tickers              │
                  │  • manage open          │
                  │    positions (TPs,      │
                  │    trailing stops)      │
                  │  • kill switch &        │
                  │    daily-loss halt      │
                  │  • adapt threshold      │
                  │    every 10 trades      │
                  └────────────┬────────────┘
                               ▼
                  ┌─────────────────────────┐
                  │ Alpaca Paper API        │
                  │  state.json (local)     │
                  │  pending_alert.json     │
                  └─────────────────────────┘
```

### Daily run sequence (`trader.py`)

1. **Load state** from `state.json` (open positions, equity high-water-mark,
   daily anchor, trade history, adaptive threshold).
2. **Bail if market closed** (unless `--force`) and bail if a kill switch
   was tripped on a prior run.
3. **Update equity anchors.** Sets the all-time high-water-mark and a fresh
   daily anchor each morning.
4. **Kill-switch check.** If equity ≤ `(1 − KILL_SWITCH_DD) × HWM`,
   liquidate every open position via market sells, set
   `kill_switch_tripped`, and stop. The bot will refuse to run again until
   you reset `state.json`.
5. **Reconcile** state with Alpaca. If a position is in `state.json` but no
   longer in Alpaca (closed manually, rejected entry, etc.), drop it and
   record a `external_close_or_unfilled` row in trade history.
6. **Manage open positions** — for each open position:
   - Pull latest IEX trade/bar price.
   - Update peak price.
   - If gain crosses the next `TP_LAYERS` threshold, sell that layer's
     fraction of the *initial* size and roll the stop forward
     (breakeven → 20% trail → 10% trail).
   - If price ≤ active stop, sell the remainder and close.
7. **Daily-loss check.** If daily P&L ≤ `−DAILY_LOSS_LIMIT`, write
   `pending_alert.json` (a snapshot of equity, drawdown, open positions,
   and today's closed trades) and skip new entries.
8. **Entry scan** (skipped if entries already ran today, unless
   `--mode entry`):
   - Pull congressional disclosures (House + Senate, with mirror fallback).
   - Aggregate by ticker, dedup buyers per household, rank by distinct
     buyer-households then total disclosed dollar amount.
   - Take the top N where N = `min(MAX_CONCURRENT − open, MAX_NEW_ENTRIES_PER_DAY)`.
   - Size each at `POSITION_PCT × buying_power`, market-buy.
9. **Adaptive tune.** After every `ADAPT_EVERY_N_TRADES` closed trades,
   look at the recent window's win rate and tighten or loosen
   `min_distinct_buyers`.
10. **Save state.**

### Strategy in one sentence

Buy what ≥ 2 different congressional households just disclosed buying;
take partial profits at +30 / +50 / +100% with progressively tighter stops;
hard-stop the whole bot at 20% daily loss or 50% peak-to-trough drawdown.

---

## Setup

### 1. Get Alpaca paper credentials

Sign up at [alpaca.markets](https://alpaca.markets), create a paper trading
key, then copy `about.txt.example` to `about.txt` and fill in your values:

```
Endpoint: https://paper-api.alpaca.markets/v2
Key:      YOUR_KEY
Secret:   YOUR_SECRET
```

`about.txt` is gitignored — it never ends up in the repo.

Alternatively, set environment variables: `APCA_KEY` (or `APCA_API_KEY_ID`)
and `APCA_SECRET` (or `APCA_API_SECRET_KEY`). The bot prefers `about.txt`
but falls back to env vars.

### 2. Install dependencies

```
pip install -r requirements.txt
```

Only one runtime dependency: `requests`.

### 3. Try a dry run

```
python trader.py --dry-run --force
```

This pulls real disclosures and prints what it would buy, without placing
any orders. Use this before scheduling.

---

## Run modes

| Command                              | What it does                                        |
| ------------------------------------ | --------------------------------------------------- |
| `python trader.py`                   | Full pass: manage exits, then entry scan if eligible |
| `python trader.py --mode manage`     | Exit / TP / stop check only                         |
| `python trader.py --mode entry`      | Force an entry pass (otherwise deduped by date)     |
| `python trader.py --dry-run`         | Log everything, place no orders                     |
| `python trader.py --force`           | Run even if the market is closed                    |

Combine them: `--mode entry --dry-run --force` will scan and rank congressional
buys at any hour without touching your account.

---

## Scheduling — daily at market open

This bot is designed to run once per market day, shortly after the open.

### Option A — Windows Task Scheduler (recommended)

State persists across runs because `state.json` lives on your disk. The bot
remembers open positions, the equity HWM, daily anchors, and the adaptive
counter — so trailing stops, the kill switch, and the learner all work as
designed.

1. Create `run_trader.bat` in the repo root:
   ```bat
   @echo off
   cd /d "%~dp0"
   python trader.py >> trader.log 2>&1
   ```
2. Open Task Scheduler → Create Basic Task.
3. Trigger: Weekly, Mon–Fri, at 9:35 AM (your local time, after the bell).
4. Action: Start a program → point to `run_trader.bat`.
5. Settings: ✅ "Wake the computer to run this task."

### Option B — Anthropic Remote Routine

Runs in a sandboxed cloud environment via the Claude Code routines feature.
Each run starts with a fresh git checkout, so `state.json` is *ephemeral*
— it does not carry over between days. **This means TP/trailing-stop
management, the equity HWM, the daily-loss anchor, and the adaptive learner
all reset each morning.** The bot effectively becomes a daily entry scanner
in this mode. Use Option A if you want the full strategy.

### Option C — cron (Linux/macOS)

```
35 13 * * 1-5  cd /path/to/repo && /usr/bin/python3 trader.py >> trader.log 2>&1
```

Adjust the hour for your timezone. EDT: 13:35 UTC, EST: 14:35 UTC. Pick one
that fires safely after 9:30 ET year-round, or use two crons.

---

## Adjustable metrics

All of these live as constants at the top of [`trader.py`](trader.py).
Edit them, save, run. No flags or env vars required.

### Sizing

| Constant                   | Default | Meaning                                                  |
| -------------------------- | ------- | -------------------------------------------------------- |
| `POSITION_PCT`             | `0.10`  | Fraction of buying power to allocate to each new entry.  |
| `MAX_CONCURRENT`           | `5`     | Max simultaneous open positions across the bot.          |
| `MAX_NEW_ENTRIES_PER_DAY`  | `3`     | Cap on new entries per calendar day. Avoids one-day pile-ons. |

### Signal

| Constant                  | Default | Meaning                                                       |
| ------------------------- | ------- | ------------------------------------------------------------- |
| `DISCLOSURE_WINDOW_DAYS`  | `1`     | Days back from today to count "today's" filings. 1 = strict.  |
| `MIN_DISTINCT_BUYERS`     | `2`     | Minimum distinct member-households (member + spouse = one) required to buy a ticker. |

### Risk

| Constant            | Default | Meaning                                                                       |
| ------------------- | ------- | ----------------------------------------------------------------------------- |
| `KILL_SWITCH_DD`    | `0.50`  | Fractional drawdown from equity HWM that triggers liquidate-all and shutdown. |
| `DAILY_LOSS_LIMIT`  | `0.20`  | If daily P&L ≤ −this, halt new entries and write `pending_alert.json`.        |

### Take-profit layers

```python
TP_LAYERS = [
    (0.30, 0.20, "breakeven"),
    (0.50, 0.20, "trail_20"),
    (1.00, 0.20, "trail_10"),
]
```

Each tuple is `(gain_pct, sell_fraction_of_initial_qty, stop_mode_after_fill)`.

- `gain_pct` — fraction over entry that triggers the layer.
- `sell_fraction_of_initial_qty` — fraction of the *initial* position to sell
  when the layer fires. With `0.20, 0.20, 0.20` you exit 60% across the three
  layers and let 40% ride.
- `stop_mode_after_fill` — what stop becomes active after the layer fires:
  - `"breakeven"` — pin the stop at entry price.
  - `"trail_20"` — trail 20% below the running peak.
  - `"trail_10"` — trail 10% below the running peak.

You can add a 4th layer (e.g. `(2.00, 0.10, "trail_10")`) — the engine
iterates the list and only fires layers it hasn't already filled.

### Adaptive learning

| Constant                  | Default | Meaning                                                          |
| ------------------------- | ------- | ---------------------------------------------------------------- |
| `ADAPT_EVERY_N_TRADES`    | `10`    | Closed-trade window over which to compute win rate.              |
| `ADAPT_BUYERS_STEP`       | `1`     | How much to nudge `min_distinct_buyers` each adapt cycle.        |
| `ADAPT_BUYERS_MIN`        | `2`     | Floor on `min_distinct_buyers` (never accept just one buyer).    |
| `ADAPT_BUYERS_MAX`        | `4`     | Ceiling on `min_distinct_buyers` (don't choke off all entries).  |

**Tuning rule:**

- Win rate < 40% over the last 10 trades → tighten (`min_distinct_buyers + 1`)
- Win rate > 60% over the last 10 trades → loosen (`min_distinct_buyers − 1`)
- Otherwise → unchanged.

The current value lives in `state.json` under `min_distinct_buyers` and
overrides the `MIN_DISTINCT_BUYERS` constant at runtime.

### Data feed

| Constant     | Default | Meaning                                                  |
| ------------ | ------- | -------------------------------------------------------- |
| `DATA_FEED`  | `"iex"` | Alpaca market-data feed. `"iex"` is free; `"sip"` requires a paid plan. |

---

## State files

All written to the project root. Both are gitignored.

### `state.json`

Persistent state across runs. Reset this file (delete it, or set
`kill_switch_tripped: false`) to re-arm the bot after a kill-switch trip.

Key fields:

- `open_positions[symbol]` — entry price, qty, peak, TP fills, current stop.
- `equity_hwm` — all-time high equity, used by the kill switch.
- `daily_anchor_date` / `daily_anchor_equity` — base for the daily loss limit.
- `min_distinct_buyers` — current adaptive threshold (overrides the constant).
- `disclosure_window_days` — adaptive (currently the bot only adapts the buyer
  count, but this is preserved for future tuning).
- `kill_switch_tripped` — boolean. When true, the bot refuses to run.
- `trade_history` — last 200 closed trades with full reasoning.
- `last_entry_date` — used to dedup the entry pass within a single day.

### `pending_alert.json`

Written when the daily loss limit fires. Contains a full snapshot:
equity, daily anchor, drawdown from HWM, open positions with peaks /
stops / unrealized P&L, and today's closed trades. Watch this file from
your alerting system of choice.

---

## Troubleshooting

**"Kill switch already tripped — bot disabled."**
Open `state.json`, set `"kill_switch_tripped": false`, save. Optionally
zero out `equity_hwm` so the new HWM is set fresh on the next run.

**"Market is closed — exiting (no-op)."**
The Alpaca clock says the market is closed. Add `--force` to bypass for
testing.

**"no qualifying entries taken".**
No tickers had ≥ `min_distinct_buyers` distinct households disclosing
within `DISCLOSURE_WINDOW_DAYS`. Either increase the window or accept that
some days simply have no signal. The output prints the top-3 candidates so
you can see how close they were.

**Position appears in Alpaca but bot ignores it.**
The reconciler only *removes* state entries that are missing from Alpaca;
it does not *add* Alpaca-only positions to state. If you opened a manual
trade outside the bot, the bot won't manage it. Add it to `state.json`
manually if you want it covered.

---

## Files

- [`trader.py`](trader.py) — main bot: orders, risk, exits, adaptive tuner.
- [`congress.py`](congress.py) — disclosure fetcher, normalizer, and aggregator.
- [`requirements.txt`](requirements.txt) — Python dependencies (`requests`).
- [`about.txt.example`](about.txt.example) — credentials template.
- `about.txt` — your live credentials (gitignored).
- `state.json` — persistent bot state (gitignored).
- `pending_alert.json` — written on daily-loss alert (gitignored).
