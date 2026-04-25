"""
Congressional disclosure data layer.

Fetches Periodic Transaction Reports (PTRs) from the public House and Senate
Stock Watcher feeds, normalizes the records, and aggregates them into ranked
ticker signals based on how many distinct member-households disclosed
purchases within a recent window.

Notes on the data
-----------------
- The STOCK Act gives members up to 45 days to file. "Today's filings" really
  means "trades executed 1-45 days ago," so this is a follow-the-flow signal,
  not a frontrunning one.
- The `owner` field flags spouse trades. We dedup on member name regardless
  of owner — a representative + their spouse buying the same ticker counts
  as one household of conviction, not two.
"""

import re
import time
from datetime import datetime, timedelta, timezone

import requests

HOUSE_URL = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
SENATE_URL = "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"
HOUSE_FALLBACK = "https://raw.githubusercontent.com/jeremiak/Disclosure-Reports/master/all_transactions.json"
SENATE_FALLBACK = "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json"

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")
_PURCHASE_TYPES = {"purchase", "purchase_full", "p", "buy"}


def _get_json(url, timeout=30):
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "congress-copytrader/1.0"})
            if r.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def fetch_disclosures(url, fallback_url=None):
    """Pull a JSON array of disclosure records. Falls back to mirror on failure.
    Returns [] if both sources fail (caller decides whether to abort)."""
    for u in (url, fallback_url):
        if not u:
            continue
        try:
            data = _get_json(u)
            if isinstance(data, list):
                return data
            print(f"  congress: {u} returned non-list response, skipping")
        except Exception as e:
            print(f"  congress: fetch failed for {u}: {e}")
    return []


def _parse_amount_range(s):
    """Parse '$1,001 - $15,000' to midpoint dollars (8000)."""
    if not s or not isinstance(s, str):
        return 0
    nums = re.findall(r"[\d,]+", s)
    vals = []
    for n in nums:
        try:
            vals.append(int(n.replace(",", "")))
        except ValueError:
            pass
    if not vals:
        return 0
    if len(vals) == 1:
        return vals[0]
    return (vals[0] + vals[1]) // 2


def _valid_ticker(t):
    if not t or not isinstance(t, str):
        return False
    t = t.strip().upper()
    if t in {"--", "N/A", "NA", "", "—"}:
        return False
    return bool(_TICKER_RE.match(t))


def normalize(record, chamber):
    """Return a normalized dict, or None if the record can't be used."""
    if not isinstance(record, dict):
        return None
    if chamber == "house":
        member = record.get("representative") or record.get("member")
    else:
        member = record.get("senator") or record.get("member")
    ticker = record.get("ticker")
    if not _valid_ticker(ticker):
        return None
    return {
        "chamber": chamber,
        "member": (member or "").strip(),
        "ticker": ticker.strip().upper(),
        "owner": (record.get("owner") or "").strip().lower(),
        "type": (record.get("type") or "").strip().lower(),
        "txn_date": record.get("transaction_date") or "",
        "disclosure_date": record.get("disclosure_date") or record.get("ptr_filing_date") or "",
        "amount_mid": _parse_amount_range(record.get("amount")),
    }


def _parse_iso_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s[: len(fmt)], fmt).date()
        except ValueError:
            continue
    return None


def filter_recent_purchases(records, days):
    """Keep only Purchase records whose disclosure_date is within the last `days`."""
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=max(days, 0))
    out = []
    for r in records:
        if r["type"] not in _PURCHASE_TYPES:
            continue
        d = _parse_iso_date(r["disclosure_date"])
        if d is None or d < cutoff or d > today:
            continue
        out.append(r)
    return out


def aggregate_signals(records):
    """Group by ticker, dedup buyers on member-name, return ranked signals."""
    by_ticker = {}
    for r in records:
        bucket = by_ticker.setdefault(r["ticker"], {
            "symbol": r["ticker"],
            "buyers": set(),
            "spouse_count": 0,
            "total_amount_mid": 0,
            "chambers": set(),
        })
        bucket["buyers"].add(r["member"])
        if "spouse" in r["owner"]:
            bucket["spouse_count"] += 1
        bucket["total_amount_mid"] += r["amount_mid"]
        bucket["chambers"].add(r["chamber"])

    signals = []
    for sym, b in by_ticker.items():
        signals.append({
            "symbol": sym,
            "distinct_buyers": len(b["buyers"]),
            "buyers": sorted(b["buyers"]),
            "spouse_count": b["spouse_count"],
            "total_amount_mid": b["total_amount_mid"],
            "chambers": sorted(b["chambers"]),
        })
    signals.sort(key=lambda s: (s["distinct_buyers"], s["total_amount_mid"]), reverse=True)
    return signals


def congress_signals(min_distinct_buyers, window_days):
    """Top-level entry: fetch -> normalize -> filter -> aggregate -> threshold."""
    print(f"  congress: fetching House feed...")
    house_raw = fetch_disclosures(HOUSE_URL, HOUSE_FALLBACK)
    print(f"  congress: House records pulled: {len(house_raw):,}")

    print(f"  congress: fetching Senate feed...")
    senate_raw = fetch_disclosures(SENATE_URL, SENATE_FALLBACK)
    print(f"  congress: Senate records pulled: {len(senate_raw):,}")

    normalized = []
    for r in house_raw:
        n = normalize(r, "house")
        if n:
            normalized.append(n)
    for r in senate_raw:
        n = normalize(r, "senate")
        if n:
            normalized.append(n)
    print(f"  congress: normalized & valid-ticker records: {len(normalized):,}")

    recent = filter_recent_purchases(normalized, window_days)
    print(f"  congress: purchases disclosed within last {window_days} day(s): {len(recent):,}")

    aggregated = aggregate_signals(recent)
    print(f"  congress: distinct tickers in window: {len(aggregated)}")

    qualifying = [s for s in aggregated if s["distinct_buyers"] >= min_distinct_buyers]
    print(f"  congress: tickers with >= {min_distinct_buyers} distinct buyer households: {len(qualifying)}")
    return qualifying
