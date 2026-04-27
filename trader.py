"""
Congressional Copy-Trading Bot
==============================
Mirrors disclosed US Representative + Senator (and spouse) stock purchases on
Alpaca paper-trading. Every market day, pulls fresh Periodic Transaction
Reports from the public House + Senate Stock Watcher feeds, picks tickers
where 2+ distinct member-households disclosed buys, and opens positions.

Strategy
--------
Universe : all tickers appearing on today's PTR filings (House + Senate).
Signal   : >= MIN_DISTINCT_BUYERS distinct member households disclosed
           Purchase transactions of the same ticker within the last
           DISCLOSURE_WINDOW_DAYS day(s). Spouse trades count as the same
           household as the member.
Sizing   : POSITION_PCT of buying power per name, max MAX_CONCURRENT
           positions, max MAX_NEW_ENTRIES_PER_DAY new entries per day.
Exits    : 3 layered take-profits (20% each at +30/+50/+100%), with stop
           progression to breakeven, then 20% trailing, then 10% trailing.
Risk     : Halt new entries if daily P&L <= -DAILY_LOSS_LIMIT.
           Kill switch (liquidate all + halt) if equity <= (1-KILL_SWITCH_DD)
           * all-time-high equity.
Adaptive : After every ADAPT_EVERY_N_TRADES closed trades, tune
           min_distinct_buyers based on recent win rate.

STOCK Act caveat
----------------
Members can file up to 45 days after a trade. "Today's filings" = trades
executed 1-45 days ago. No real-time edge; this is a follow-the-flow
strategy, not a frontrunning one.

Run examples
------------
    python trader.py --dry-run --force      # full scan, no orders
    python trader.py                        # entry + exit pass, market-hours only
    python trader.py --mode manage          # exit/TP/stop check only
    python trader.py --mode entry           # force an entry pass (dedup'd by date)
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests

import congress

# ── credentials ──────────────────────────────────────────────────────────────

def _load_creds():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "about.txt")
    key = secret = None
    if os.path.exists(path):
        kv = {}
        with open(path) as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    kv[k.strip().lower()] = v.strip()
        key = kv.get("key")
        secret = kv.get("secret")
    if not key or not secret:
        key = os.environ.get("APCA_KEY") or os.environ.get("APCA_API_KEY_ID")
        secret = os.environ.get("APCA_SECRET") or os.environ.get("APCA_API_SECRET_KEY")
    if not (key and secret):
        sys.exit("ERROR: no Alpaca credentials found in about.txt or env vars")
    return key, secret


APCA_KEY, APCA_SECRET = _load_creds()
BASE_URL = "https://paper-api.alpaca.markets/v2"
DATA_URL = "https://data.alpaca.markets/v2"
HEADERS = {
    "APCA-API-KEY-ID": APCA_KEY,
    "APCA-API-SECRET-KEY": APCA_SECRET,
}
DATA_FEED = "iex"  # free tier

# ── config (all knobs) ───────────────────────────────────────────────────────

# Sizing
POSITION_PCT = 0.10            # fraction of buying power per ticker
MAX_CONCURRENT = 5             # max simultaneous open positions
MAX_NEW_ENTRIES_PER_DAY = 3    # cap to avoid a Pelosi-dump explosion

# Signal
DISCLOSURE_WINDOW_DAYS = 7     # how many days back to count "today's" filings (filings batch, 1 was too tight)
MIN_DISTINCT_BUYERS = 2        # household-dedup'd member count required

# Risk
KILL_SWITCH_DD = 0.50          # liquidate + halt at 50% drawdown from HWM
DAILY_LOSS_LIMIT = 0.20        # halt new entries if down 20% on the day

# (gain_pct, sell_fraction_of_initial, stop_mode_after_fill)
TP_LAYERS = [
    (0.30, 0.20, "breakeven"),
    (0.50, 0.20, "trail_20"),
    (1.00, 0.20, "trail_10"),
]

# Adaptive learning
ADAPT_EVERY_N_TRADES = 10
ADAPT_BUYERS_STEP = 1
ADAPT_BUYERS_MIN = 2
ADAPT_BUYERS_MAX = 4

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
ALERT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_alert.json")

# ── tiny REST helpers ────────────────────────────────────────────────────────

def _req(method, url, **kw):
    for attempt in range(3):
        try:
            r = requests.request(method, url, headers=HEADERS, timeout=20, **kw)
            if r.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json() if r.text else {}
        except requests.exceptions.RequestException:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def api(method, path, **kw):
    return _req(method, BASE_URL + path, **kw)


def data(path, **kw):
    return _req("GET", DATA_URL + path, **kw)


# ── market data ──────────────────────────────────────────────────────────────

def market_open():
    clock = api("GET", "/clock")
    return bool(clock.get("is_open"))


def latest_price(symbol):
    j = data(f"/stocks/{symbol}/trades/latest", params={"feed": DATA_FEED})
    px = (j.get("trade") or {}).get("p")
    if px:
        return float(px)
    j = data(f"/stocks/{symbol}/bars/latest", params={"feed": DATA_FEED})
    return float((j.get("bar") or {}).get("c") or 0.0)


# ── state ────────────────────────────────────────────────────────────────────

DEFAULT_STATE = {
    "open_positions": {},
    "trade_history": [],
    "equity_hwm": None,
    "daily_anchor_date": None,
    "daily_anchor_equity": None,
    "min_distinct_buyers": MIN_DISTINCT_BUYERS,
    "disclosure_window_days": DISCLOSURE_WINDOW_DAYS,
    "kill_switch_tripped": False,
    "last_run": None,
    "last_entry_date": None,
    "trades_at_last_adapt": 0,
    "last_alert_date": None,
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        for k, v in DEFAULT_STATE.items():
            s.setdefault(k, v)
        return s
    return dict(DEFAULT_STATE)


def save_state(s):
    s["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2, default=str)


# ── account / risk ───────────────────────────────────────────────────────────

def get_account():
    return api("GET", "/account")


def update_equity_anchors(state, account):
    equity = float(account["equity"])
    if state["equity_hwm"] is None or equity > state["equity_hwm"]:
        state["equity_hwm"] = equity
    today = datetime.now(timezone.utc).date().isoformat()
    if state["daily_anchor_date"] != today:
        state["daily_anchor_date"] = today
        state["daily_anchor_equity"] = equity
    return equity


def daily_pnl_pct(state, equity):
    anchor = state.get("daily_anchor_equity")
    if not anchor:
        return 0.0
    return (equity - anchor) / anchor


def kill_switch(state, equity):
    hwm = state.get("equity_hwm") or equity
    return equity <= hwm * (1.0 - KILL_SWITCH_DD)


# ── position sync with Alpaca ────────────────────────────────────────────────

def alpaca_positions():
    out = {}
    for p in api("GET", "/positions"):
        out[p["symbol"]] = {
            "qty": float(p["qty"]),
            "avg_entry": float(p["avg_entry_price"]),
            "current_price": float(p["current_price"]),
            "market_value": float(p["market_value"]),
        }
    return out


def reconcile(state, live_positions):
    """Drop state entries not in Alpaca (manual closure or rejected entry)."""
    for sym in list(state["open_positions"].keys()):
        if sym not in live_positions:
            pos = state["open_positions"].pop(sym)
            print(f"  reconcile: {sym} not in Alpaca — assuming externally closed or "
                  f"unfilled, removing from state")
            state["trade_history"].append({
                "symbol": sym,
                "entry_price": pos.get("entry_price"),
                "exit_price": None,
                "qty_initial": pos.get("qty_initial"),
                "qty_remaining": 0,
                "pnl_pct": None,
                "opened_at": pos.get("entry_time"),
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "reason": "external_close_or_unfilled",
                "entry_reasoning": pos.get("entry_reasoning"),
                "tp_fills": pos.get("tp_fills", []),
                "exit_details": {},
            })
            state["trade_history"] = state["trade_history"][-200:]


# ── daily loss alert ─────────────────────────────────────────────────────────

def write_pending_alert(state, account, equity, daily_pnl, live_positions):
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("last_alert_date") == today:
        print(f"  daily-loss alert already written today ({today}) — skipping duplicate")
        return
    anchor = state.get("daily_anchor_equity") or equity
    hwm = state.get("equity_hwm") or equity
    drawdown_from_hwm = (equity - hwm) / hwm if hwm else 0

    todays = [
        t for t in state["trade_history"]
        if t.get("closed_at", "").startswith(today)
    ]

    open_snapshot = []
    for sym, pos in state["open_positions"].items():
        live = live_positions.get(sym, {})
        current_px = live.get("current_price")
        entry = pos.get("entry_price")
        unreal_pct = ((current_px - entry) / entry) if (current_px and entry) else None
        open_snapshot.append({
            "symbol": sym,
            "entry_price": entry,
            "current_price": current_px,
            "qty_remaining": pos.get("qty_remaining"),
            "qty_initial": pos.get("qty_initial"),
            "unrealized_pnl_pct": unreal_pct,
            "peak_price": pos.get("peak_price"),
            "stop_price": pos.get("stop_price"),
            "stop_mode": pos.get("stop_mode"),
            "tp_layers_filled": len(pos.get("tp_fills", [])),
            "entry_time": pos.get("entry_time"),
            "entry_reasoning": pos.get("entry_reasoning"),
        })

    alert = {
        "date": today,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "account_number": account.get("account_number"),
        "equity": equity,
        "daily_anchor_equity": anchor,
        "daily_pnl_dollars": equity - anchor,
        "daily_pnl_pct": daily_pnl,
        "daily_loss_limit_pct": -DAILY_LOSS_LIMIT,
        "equity_hwm": hwm,
        "drawdown_from_hwm_pct": drawdown_from_hwm,
        "kill_switch_threshold_pct": -KILL_SWITCH_DD,
        "kill_switch_tripped": state.get("kill_switch_tripped", False),
        "adaptive_min_distinct_buyers": state.get("min_distinct_buyers"),
        "open_positions": open_snapshot,
        "todays_closed_trades": todays,
    }
    with open(ALERT_FILE, "w") as f:
        json.dump(alert, f, indent=2, default=str)
    state["last_alert_date"] = today
    print(f"  ALERT written to {ALERT_FILE}: daily P&L {daily_pnl*100:.2f}%")


# ── orders ───────────────────────────────────────────────────────────────────

def submit_market(symbol, qty, side, dry_run):
    if dry_run:
        print(f"  [dry-run] {side.upper()} {qty} {symbol} (market)")
        return None
    body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    return api("POST", "/orders", json=body)


# ── exits / position management ──────────────────────────────────────────────

def manage_position(state, symbol, dry_run):
    pos = state["open_positions"][symbol]
    entry = pos["entry_price"]
    qty_remaining = pos["qty_remaining"]
    if qty_remaining <= 0:
        return

    px = latest_price(symbol)
    if not px:
        return
    pos["peak_price"] = max(pos.get("peak_price", entry), px)
    gain_pct = (px - entry) / entry

    # 1. layered take-profits — at most one fires per run
    for i, (tp_pct, sell_frac, stop_mode) in enumerate(TP_LAYERS):
        if i in pos["tp_filled"]:
            continue
        if gain_pct >= tp_pct:
            qty_to_sell = math.floor(pos["qty_initial"] * sell_frac)
            qty_to_sell = min(qty_to_sell, qty_remaining)
            if qty_to_sell > 0:
                qty_after = qty_remaining - qty_to_sell
                stop_after = _compute_stop({**pos, "stop_price": None}, px, stop_mode)
                print(f"  TP{i+1} hit on {symbol}: SELL {qty_to_sell} shares @ ${px:.2f} "
                      f"(gain +{gain_pct*100:.1f}%, layer {i+1} of {len(TP_LAYERS)})")
                print(f"    Reasoning: take-profit threshold +{tp_pct*100:.0f}% reached")
                print(f"      - Entry ${entry:.2f} -> current ${px:.2f} (+{gain_pct*100:.2f}%)")
                print(f"      - Sold {qty_to_sell} / {pos['qty_initial']} shares (initial), "
                      f"{qty_after} remaining")
                print(f"      - Peak so far: ${pos['peak_price']:.2f}")
                print(f"      - Stop progression: {pos.get('stop_mode') or 'none'} -> "
                      f"{stop_mode} (stop ${stop_after:.2f})")
                submit_market(symbol, qty_to_sell, "sell", dry_run)
                pos["qty_remaining"] = qty_after
                pos["tp_filled"].append(i)
                pos["stop_mode"] = stop_mode
                pos["stop_price"] = stop_after
                pos.setdefault("tp_fills", []).append({
                    "tp_layer": i + 1,
                    "threshold_pct": tp_pct,
                    "gain_pct_at_fill": gain_pct,
                    "qty_sold": qty_to_sell,
                    "price": px,
                    "qty_remaining_after": qty_after,
                    "stop_mode_after": stop_mode,
                    "stop_price_after": stop_after,
                    "peak_at_fill": pos["peak_price"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            break

    # 2. trailing stop / breakeven check
    if pos.get("stop_price") and pos.get("stop_mode"):
        prev_stop = pos["stop_price"]
        if pos["stop_mode"] == "trail_20":
            pos["stop_price"] = max(pos["stop_price"], pos["peak_price"] * (1 - 0.20))
        elif pos["stop_mode"] == "trail_10":
            pos["stop_price"] = max(pos["stop_price"], pos["peak_price"] * (1 - 0.10))
        if pos["stop_price"] != prev_stop:
            print(f"  trail update on {symbol}: stop ${prev_stop:.2f} -> ${pos['stop_price']:.2f} "
                  f"(peak ${pos['peak_price']:.2f}, mode {pos['stop_mode']})")
        if px <= pos["stop_price"] and pos["qty_remaining"] > 0:
            qty = pos["qty_remaining"]
            drop_from_peak = (px / pos["peak_price"]) - 1 if pos["peak_price"] else 0
            tp_count = len(pos.get("tp_fills", []))
            print(f"  STOP hit on {symbol}: SELL {qty} shares @ ${px:.2f} "
                  f"(realized P&L on remaining +{gain_pct*100:.1f}%)")
            print(f"    Reasoning: {pos['stop_mode']} stop ${pos['stop_price']:.2f} breached")
            print(f"      - Entry ${entry:.2f}, peak ${pos['peak_price']:.2f}, exit ${px:.2f}")
            print(f"      - Drop from peak: {drop_from_peak*100:+.2f}%")
            print(f"      - {tp_count} of {len(TP_LAYERS)} take-profit layers had filled before stop")
            submit_market(symbol, qty, "sell", dry_run)
            _close(state, symbol, px, "stop", details={
                "stop_mode": pos["stop_mode"],
                "stop_price": pos["stop_price"],
                "peak_price": pos["peak_price"],
                "drop_from_peak_pct": drop_from_peak,
                "tp_fills": pos.get("tp_fills", []),
                "qty_at_stop": qty,
            })


def _compute_stop(pos, px, mode):
    if mode == "breakeven":
        return pos["entry_price"]
    if mode == "trail_20":
        return pos["peak_price"] * (1 - 0.20)
    if mode == "trail_10":
        return pos["peak_price"] * (1 - 0.10)
    return None


def _close(state, symbol, exit_price, reason, details=None):
    pos = state["open_positions"].pop(symbol)
    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] if pos.get("entry_price") else None
    record = {
        "symbol": symbol,
        "entry_price": pos.get("entry_price"),
        "exit_price": exit_price,
        "qty_initial": pos.get("qty_initial"),
        "qty_remaining": 0,
        "pnl_pct": pnl_pct,
        "opened_at": pos.get("entry_time"),
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "entry_reasoning": pos.get("entry_reasoning"),
        "tp_fills": pos.get("tp_fills", []),
        "exit_details": details or {},
    }
    state["trade_history"].append(record)
    state["trade_history"] = state["trade_history"][-200:]


# ── liquidate (kill switch) ──────────────────────────────────────────────────

def liquidate_all(state, dry_run):
    for sym, pos in list(state["open_positions"].items()):
        if pos.get("qty_remaining", 0) > 0:
            qty = pos["qty_remaining"]
            px = latest_price(sym) or pos["entry_price"]
            entry = pos["entry_price"]
            gain_pct = (px - entry) / entry if entry else 0
            print(f"  KILL SWITCH on {sym}: SELL {qty} shares @ ${px:.2f} "
                  f"(P&L on remainder {gain_pct*100:+.1f}%)")
            print(f"    Reasoning: 50% drawdown from equity high-water-mark triggered")
            print(f"      - Entry ${entry:.2f}, peak ${pos.get('peak_price', entry):.2f}, "
                  f"exit ${px:.2f}")
            print(f"      - {len(pos.get('tp_fills', []))} TP layers had filled before liquidation")
            submit_market(sym, qty, "sell", dry_run)
            _close(state, sym, px, "kill_switch", details={
                "peak_price": pos.get("peak_price"),
                "tp_fills": pos.get("tp_fills", []),
                "qty_at_kill": qty,
            })
    state["kill_switch_tripped"] = True


# ── entry ────────────────────────────────────────────────────────────────────

def enter_position(state, signal, account, dry_run, rank, candidates_total):
    sym = signal["symbol"]
    bp = float(account["buying_power"])
    notional = bp * POSITION_PCT
    px = latest_price(sym)
    if not px:
        print(f"  skip {sym}: could not fetch latest price")
        return False
    qty = math.floor(notional / px)
    if qty < 1:
        print(f"  skip {sym}: position size < 1 share (bp={bp:.0f}, px={px:.2f})")
        return False

    threshold = state.get("min_distinct_buyers", MIN_DISTINCT_BUYERS)
    window = state.get("disclosure_window_days", DISCLOSURE_WINDOW_DAYS)
    reasoning = {
        "distinct_buyers": signal["distinct_buyers"],
        "min_distinct_buyers": threshold,
        "buyers": signal["buyers"],
        "spouse_count": signal["spouse_count"],
        "total_amount_mid": signal["total_amount_mid"],
        "chambers": signal["chambers"],
        "disclosure_window_days": window,
        "rank": rank,
        "candidates_total": candidates_total,
        "buying_power": bp,
        "qty": qty,
        "price": px,
        "notional": qty * px,
        "pct_of_buying_power": POSITION_PCT,
    }

    print(f"  ENTER {sym}: BUY {qty} shares @ ${px:.2f} (notional ${qty*px:,.0f}, "
          f"{POSITION_PCT*100:.0f}% of ${bp:,.0f} BP)")
    print(f"    Reasoning: ranked #{rank} of {candidates_total} qualifying signals")
    print(f"      - {signal['distinct_buyers']} distinct member households disclosed Purchases "
          f"(threshold: {threshold}+)")
    buyers_str = ", ".join(signal["buyers"][:5])
    if len(signal["buyers"]) > 5:
        buyers_str += f", +{len(signal['buyers'])-5} more"
    spouse_note = f" ({signal['spouse_count']} spouse trade(s) included)" if signal["spouse_count"] else ""
    print(f"      - Buyers: {buyers_str}{spouse_note}")
    print(f"      - Total disclosed mid-amount: ${signal['total_amount_mid']:,}")
    print(f"      - Chamber(s): {', '.join(signal['chambers'])}")
    print(f"      - Filings within last {window} day(s)")

    submit_market(sym, qty, "buy", dry_run)
    state["open_positions"][sym] = {
        "entry_price": px,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "qty_initial": qty,
        "qty_remaining": qty,
        "peak_price": px,
        "tp_filled": [],
        "tp_fills": [],
        "stop_mode": None,
        "stop_price": None,
        "entry_reasoning": reasoning,
    }
    return True


# ── adaptive learning ────────────────────────────────────────────────────────

def maybe_adapt(state):
    closed = [t for t in state["trade_history"] if t.get("pnl_pct") is not None]
    n_since = len(closed) - state.get("trades_at_last_adapt", 0)
    if n_since < ADAPT_EVERY_N_TRADES:
        return
    recent = closed[-ADAPT_EVERY_N_TRADES:]
    wins = sum(1 for t in recent if t["pnl_pct"] > 0)
    wr = wins / len(recent)
    cur = state.get("min_distinct_buyers", MIN_DISTINCT_BUYERS)
    print(f"Adaptive tune: {len(recent)} recent trades, win rate {wr*100:.0f}%")
    if wr < 0.4:
        new = min(cur + ADAPT_BUYERS_STEP, ADAPT_BUYERS_MAX)
        state["min_distinct_buyers"] = new
        print(f"  tightened: min_distinct_buyers {cur} -> {new}")
    elif wr > 0.6:
        new = max(cur - ADAPT_BUYERS_STEP, ADAPT_BUYERS_MIN)
        state["min_distinct_buyers"] = new
        print(f"  loosened:  min_distinct_buyers {cur} -> {new}")
    else:
        print("  unchanged")
    state["trades_at_last_adapt"] = len(closed)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("entry", "manage", "both"), default="both",
                    help="entry: run signal scan only; manage: run exit logic only; both: full pass")
    ap.add_argument("--dry-run", action="store_true",
                    help="scan + log signals without placing orders")
    ap.add_argument("--force", action="store_true",
                    help="run even if market is closed (for testing)")
    args = ap.parse_args()

    print(f"=== Congressional Copy-Trader @ {datetime.now(timezone.utc).isoformat()} ===")
    print(f"Mode: {args.mode}{' (DRY-RUN)' if args.dry_run else ''}")

    state = load_state()
    if state.get("kill_switch_tripped"):
        print("Kill switch already tripped — bot disabled. Reset state.json to resume.")
        return

    if not args.force and not market_open():
        print("Market is closed — exiting (no-op).")
        save_state(state)
        return

    account = get_account()
    if account.get("trading_blocked") or account.get("account_blocked"):
        print(f"Alpaca account blocked: trading_blocked={account.get('trading_blocked')}, "
              f"account_blocked={account.get('account_blocked')}")
        return

    equity = update_equity_anchors(state, account)
    print(f"Equity ${equity:,.2f} | HWM ${state['equity_hwm']:,.2f} | "
          f"day anchor ${state['daily_anchor_equity']:,.2f}")

    if kill_switch(state, equity):
        print(f"KILL SWITCH: equity {equity:.0f} <= {(1-KILL_SWITCH_DD)*100:.0f}% of HWM "
              f"{state['equity_hwm']:.0f}")
        liquidate_all(state, args.dry_run)
        save_state(state)
        return

    live = alpaca_positions()
    reconcile(state, live)

    print(f"\nManaging {len(state['open_positions'])} open position(s)...")
    for sym in list(state["open_positions"].keys()):
        manage_position(state, sym, args.dry_run)

    daily_pnl = daily_pnl_pct(state, equity)
    today = datetime.now(timezone.utc).date().isoformat()

    if daily_pnl <= -DAILY_LOSS_LIMIT:
        print(f"Daily loss limit hit ({daily_pnl*100:.1f}%) — no new entries.")
        write_pending_alert(state, account, equity, daily_pnl, live)
    elif args.mode in ("entry", "both"):
        if state.get("last_entry_date") == today and args.mode == "both":
            print(f"\nEntries already executed today ({today}) — skipping entry scan. "
                  f"Use --mode entry to force.")
        else:
            slots = MAX_CONCURRENT - len(state["open_positions"])
            slots = min(slots, MAX_NEW_ENTRIES_PER_DAY)
            if slots <= 0:
                print(f"\nNo entry slots available "
                      f"(open={len(state['open_positions'])}, max={MAX_CONCURRENT}, "
                      f"daily-cap={MAX_NEW_ENTRIES_PER_DAY}).")
            else:
                print(f"\nScanning congressional disclosures ({slots} slot(s) available)...")
                signals = congress.congress_signals(
                    state.get("min_distinct_buyers", MIN_DISTINCT_BUYERS),
                    state.get("disclosure_window_days", DISCLOSURE_WINDOW_DAYS),
                )
                already = set(state["open_positions"].keys())
                taken = 0
                for rank, sig in enumerate(signals, start=1):
                    if taken >= slots:
                        break
                    if sig["symbol"] in already:
                        print(f"  skip {sig['symbol']}: already an open position")
                        continue
                    acct_now = get_account() if taken > 0 and not args.dry_run else account
                    if enter_position(state, sig, acct_now, args.dry_run, rank, len(signals)):
                        taken += 1
                if taken == 0:
                    print(f"  no qualifying entries taken (top-3 of {len(signals)} candidates):")
                    for sig in signals[:3]:
                        print(f"    {sig['symbol']}: {sig['distinct_buyers']} buyers, "
                              f"${sig['total_amount_mid']:,} disclosed")
                state["last_entry_date"] = today
    else:
        print(f"\nMode '{args.mode}' — entry scan skipped.")

    maybe_adapt(state)
    save_state(state)
    print("=== Run complete ===")


if __name__ == "__main__":
    main()
