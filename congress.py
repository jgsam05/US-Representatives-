"""
Congressional disclosure data layer.

Pulls Periodic Transaction Reports (PTRs) from the kadoa-org congress-trading-
monitor feed (a MIT-licensed aggregator of House Clerk and Senate eFD filings),
normalizes records into a common shape, filters for recent Purchases, and
aggregates them into ranked ticker signals based on how many distinct
member-households disclosed buys within a recent window.

Data source
-----------
Primary: https://github.com/kadoa-org/congress-trading-monitor (MIT)
         — public/data/trades.json, refreshed regularly with both House and
         Senate filings in one file. Includes filer name, chamber, owner
         (self/spouse/joint/dependent-child), filing_date, transaction_date,
         transaction_type, ticker, and numeric amount range.

The previously-used Stock Watcher S3 buckets and the jeremiak GitHub mirror
are no longer reachable (403/404) as of 2026-04, hence the rewire.

Notes on the data
-----------------
- The STOCK Act gives members up to 45 days to file. "Today's filings" really
  means "trades executed 1-45 days ago," so this is a follow-the-flow signal,
  not a frontrunning one.
- We dedup on `filer_name` regardless of owner — a representative + their
  spouse buying the same ticker counts as one household of conviction, not
  two. The `owner` field is preserved for the spouse-count metric only.
"""

import re
import time
from datetime import datetime, timedelta, timezone

import requests

KADOA_URL = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")
_PURCHASE_TYPES = {"purchase", "purchase_full", "p", "buy"}
_VALID_CHAMBERS = {"house", "senate"}
_SPOUSE_OWNER_CODES = {"sp", "spouse"}


def _get_json(url, timeout=60):
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


def fetch_disclosures(url):
    """Pull a JSON array of disclosure records. Returns [] on any failure
    so the caller can degrade gracefully rather than crash."""
    try:
        data = _get_json(url)
        if isinstance(data, list):
            return data
        print(f"  congress: {url} returned non-list response, skipping")
    except Exception as e:
        print(f"  congress: fetch failed for {url}: {e}")
    return []


def _valid_ticker(t):
    if not t or not isinstance(t, str):
        return False
    t = t.strip().upper()
    if t in {"--", "N/A", "NA", "", "—"}:
        return False
    return bool(_TICKER_RE.match(t))


def _amount_mid(record):
    """Midpoint of the disclosed dollar range. Prefer numeric fields when
    present (kadoa schema), fall back to parsing a label string."""
    lo = record.get("amount_range_low")
    hi = record.get("amount_range_high")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        return int((lo + hi) // 2)
    if isinstance(lo, (int, float)):
        return int(lo)
    label = record.get("amount_range_label") or record.get("amount") or ""
    if not isinstance(label, str):
        return 0
    nums = re.findall(r"[\d,]+", label)
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


def normalize(record):
    """Return a normalized dict, or None if the record can't be used.

    Maps the kadoa schema (filer_name, chamber, filing_date, transaction_type,
    amount_range_low/high) into the internal shape used by aggregate_signals."""
    if not isinstance(record, dict):
        return None
    chamber = (record.get("chamber") or "").strip().lower()
    if chamber not in _VALID_CHAMBERS:
        return None
    ticker = record.get("ticker")
    if not _valid_ticker(ticker):
        return None
    member = record.get("filer_name") or record.get("member") or ""
    return {
        "chamber": chamber,
        "member": member.strip(),
        "ticker": ticker.strip().upper(),
        "owner": (record.get("owner") or "").strip().lower(),
        "type": (record.get("transaction_type") or record.get("type") or "").strip().lower(),
        "txn_date": record.get("transaction_date") or "",
        "disclosure_date": record.get("filing_date") or record.get("disclosure_date") or "",
        "amount_mid": _amount_mid(record),
    }


def _parse_iso_date(s):
    if not s or not isinstance(s, str):
        return None
    s = s.strip().rstrip("Z")
    # Try ISO 8601 first (handles both "YYYY-MM-DD" and "YYYY-MM-DDTHH:MM:SS")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    # Fallback for US-style "M/D/YYYY"
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
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
        if r["owner"] in _SPOUSE_OWNER_CODES:
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
    print(f"  congress: fetching kadoa trades feed...")
    raw = fetch_disclosures(KADOA_URL)
    print(f"  congress: records pulled: {len(raw):,}")

    normalized = []
    for r in raw:
        n = normalize(r)
        if n:
            normalized.append(n)

    house_n = sum(1 for r in normalized if r["chamber"] == "house")
    senate_n = sum(1 for r in normalized if r["chamber"] == "senate")
    print(f"  congress: normalized & valid-ticker records: {len(normalized):,} "
          f"(house {house_n:,}, senate {senate_n:,})")

    recent = filter_recent_purchases(normalized, window_days)
    print(f"  congress: purchases disclosed within last {window_days} day(s): {len(recent):,}")

    aggregated = aggregate_signals(recent)
    print(f"  congress: distinct tickers in window: {len(aggregated)}")

    qualifying = [s for s in aggregated if s["distinct_buyers"] >= min_distinct_buyers]
    print(f"  congress: tickers with >= {min_distinct_buyers} distinct buyer households: {len(qualifying)}")
    return qualifying
