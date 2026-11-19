"""Independent A/B PAPER simulator. Never places real orders."""
import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
from decimal import Decimal
import requests

WALLET = "0xf0318c32136c2db7fec88b84869aee6a1106c80c"
API = "https://data-api.polymarket.com/v2/trades"
DB = os.getenv("AB_DB_PATH", "/data/portfolios_ab.sqlite3")
START = Decimal("1000")
SOURCE_BANKROLL = Decimal(os.getenv("AB_SOURCE_BANKROLL", "1000000"))
EQUAL_BUY = Decimal(os.getenv("AB_EQUAL_BUY", "10"))
CAP = Decimal(os.getenv("AB_MARKET_CAP", "100"))
POLL = max(10, int(os.getenv("AB_POLL_SECONDS", "30")))
DURATION = int(os.getenv("AB_HOURS", "72"))
SUMMARY = int(os.getenv("AB_SUMMARY_HOURS", "12"))
MAX_PAGES = max(1, int(os.getenv("AB_MAX_PAGES", "30")))
UTC = dt.timezone.utc

def clock():
    return dt.datetime.now(UTC)

def dec(value):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")

def meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def setmeta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

def trade_id(trade):
    fields = {k: trade.get(k) for k in ("transaction_hash", "token_id", "condition_id", "side", "price", "size", "timestamp", "outcome")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()

def fetch_recent():
    """Fetch all pages until exhausted; fail closed if the page cap is reached."""
    cursor, cursors, result = None, set(), []
    for page in range(MAX_PAGES):
        params = {"user": WALLET, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        response = requests.get(API, params=params, timeout=30)
        response.raise_for_status()
        obj = response.json()
        if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
            raise ValueError("Unexpected Polymarket response")
        result.extend(obj["data"])
        pagination = obj.get("pagination") or {}
        if not pagination.get("has_more"):
            return result
        next_cursor = pagination.get("next_cursor")
        if not next_cursor or next_cursor in cursors:
            raise ValueError("Incomplete pagination; refusing to process partial batch")
        cursors.add(next_cursor)
        cursor = next_cursor
    raise ValueError("Pagination exceeded AB_MAX_PAGES; no partial processing")

def connect():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, timestamp INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS source_positions (asset TEXT PRIMARY KEY, shares TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS positions (portfolio TEXT, asset TEXT, title TEXT, outcome TEXT, shares TEXT, cost TEXT, mark TEXT, PRIMARY KEY(portfolio,asset))")
    db.execute("CREATE TABLE IF NOT EXISTS fills (portfolio TEXT, id TEXT, side TEXT, asset TEXT, shares TEXT, cash_delta TEXT, note TEXT, PRIMARY KEY(portfolio,id))")
    db.commit()
    return db

def cash(db, portfolio):
    return dec(meta(db, "cash_" + portfolio, START))

def source_shares(db, asset):
    row = db.execute("SELECT shares FROM source_positions WHERE asset=?", (asset,)).fetchone()
    return dec(row[0]) if row else Decimal("0")

def process(db, trade):
    side = str(trade.get("side", "")).upper()
    asset = str(trade.get("token_id") or "")
    price = dec(trade.get("price"))
    qty = dec(trade.get("size"))
    if not asset or side not in ("BUY", "SELL") or not (0 < price < 1) or qty <= 0:
        return
    source_before = source_shares(db, asset)
    fraction = min(Decimal("1"), qty / source_before) if side == "SELL" and source_before else Decimal("0")
    source_after = source_before + qty if side == "BUY" else max(Decimal("0"), source_before - qty)
    db.execute("INSERT OR REPLACE INTO source_positions VALUES (?,?)", (asset, str(source_after)))
    uid = trade_id(trade)
    title = str(trade.get("title") or "Unknown")
    outcome = str(trade.get("outcome") or "?")
    for portfolio in ("A", "B"):
        row = db.execute("SELECT shares,cost FROM positions WHERE portfolio=? AND asset=?", (portfolio, asset)).fetchone()
        shares, cost = (dec(row[0]), dec(row[1])) if row else (Decimal("0"), Decimal("0"))
        delta, fill, note = Decimal("0"), Decimal("0"), ""
        if side == "BUY":
            desired = qty * price * START / SOURCE_BANKROLL if portfolio == "A" else EQUAL_BUY
            spend = min(desired, max(Decimal("0"), CAP-cost), cash(db, portfolio))
            if spend > 0:
                fill = spend / price
                shares += fill
                cost += spend
                delta = -spend
            else:
                note = "No cash or market capacity"
        else:
            fill = shares * fraction
            if fill > 0:
                avg_cost = cost / shares
                shares -= fill
                cost -= avg_cost * fill
                delta = fill * price
            else:
                note = "No eligible source-matched holding"
        if shares <= Decimal("0.000000001"):
            db.execute("DELETE FROM positions WHERE portfolio=? AND asset=?", (portfolio, asset))
        else:
            db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?)",
                       (portfolio, asset, title, outcome, str(shares), str(max(cost, Decimal("0"))), str(price)))
        if delta:
            setmeta(db, "cash_" + portfolio, cash(db, portfolio) + delta)
        db.execute("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?)",
                   (portfolio, uid, side, asset, str(fill), str(delta), note))

def report(db):
    lines = ["A/B PAPER ONLY — proxy trade marks; no fees, slippage or settlements"]
    for portfolio in ("A", "B"):
        marked = sum((dec(sh) * dec(price) for sh, price in db.execute(
            "SELECT shares,mark FROM positions WHERE portfolio=?", (portfolio,))), Decimal("0"))
        available = cash(db, portfolio)
        equity = available + marked
        count = db.execute("SELECT COUNT(*) FROM fills WHERE portfolio=? AND shares!='0'", (portfolio,)).fetchone()[0]
        lines.append(f"{portfolio}: cash ${available:.2f} | proxy equity ${equity:.2f} | P/L ${equity-START:+.2f} | fills {count}")
    return "\n".join(lines)

def notify(message):
    print(message, flush=True)
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat, "text": message}, timeout=15)
        response.raise_for_status()

def tick(db):
    rows = fetch_recent()
    if meta(db, "baseline_complete") != "yes":
        # The initial snapshot is a baseline, not a source of live simulated orders.
        for trade in rows:
            db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (trade_id(trade), int(trade.get("timestamp") or 0)))
        started = clock()
        setmeta(db, "started_at", started.isoformat())
        setmeta(db, "last_summary", started.isoformat())
        for portfolio in ("A", "B"):
            setmeta(db, "cash_" + portfolio, START)
        setmeta(db, "baseline_complete", "yes")
        db.commit()
        notify(f"A/B baseline ready: {len(rows)} historical trades ignored. Both portfolios start at $1000 PAPER cash.")
        return
    started = dt.datetime.fromisoformat(meta(db, "started_at"))
    active = (clock() - started).total_seconds() < DURATION * 3600
    # Only act on trades executed after the baseline timestamp.
    for trade in sorted(rows, key=lambda x: int(x.get("timestamp") or 0)):
        uid = trade_id(trade)
        timestamp = int(trade.get("timestamp") or 0)
        fresh = db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (uid, timestamp)).rowcount
        if fresh and active and timestamp > int(started.timestamp()):
            process(db, trade)
    db.commit()
    last = dt.datetime.fromisoformat(meta(db, "last_summary"))
    if (clock() - last).total_seconds() >= SUMMARY * 3600:
        notify(report(db))
        setmeta(db, "last_summary", clock().isoformat())
        db.commit()
    if not active and meta(db, "finished") != "yes":
        notify("A/B PAPER EXPERIMENT COMPLETE\n" + report(db))
        setmeta(db, "finished", "yes")
        db.commit()

def main():
    if SOURCE_BANKROLL <= 0 or EQUAL_BUY <= 0 or CAP <= 0:
        raise ValueError("Sizing parameters must be positive")
    db = connect()
    while True:
        try:
            tick(db)
        except Exception as exc:
            print("A/B ERROR:", repr(exc), flush=True)
        time.sleep(POLL)

if __name__ == "__main__":
    main()
