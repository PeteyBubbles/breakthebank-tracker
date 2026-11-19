"""Read-only market data and two independent paper ledgers."""
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import requests

WALLET = "0xf0318c32136c2db7fec88b84869aee6a1106c80c"
TRADES = "https://data-api.polymarket.com/v2/trades"
MARKETS = "https://clob.polymarket.com/markets/"
START = Decimal("1000")
ZERO = Decimal("0")
DELAY = 120
OVERLAP = 3600


@dataclass(frozen=True)
class Trade:
    id: str
    timestamp: int
    condition: str
    asset: str
    side: str
    price: Decimal
    size: Decimal

    @classmethod
    def parse(cls, row):
        stamp = Decimal(str(row["timestamp"]))
        if stamp >= 10**12:
            stamp /= 1000
        if not 10**9 <= stamp < 10**10:
            raise ValueError("Invalid trade timestamp")
        price, size = Decimal(str(row["price"])), Decimal(str(row["size"]))
        side = row["side"].upper()
        tx, condition, asset = (str(row[key]).lower() for key in
                                ("transaction_hash", "condition_id", "token_id"))
        if (not 0 < price <= 1 or not size.is_finite() or size <= 0
                or side not in ("BUY", "SELL") or not tx.startswith("0x")
                or not condition.startswith("0x") or not asset.isdigit()):
            raise ValueError("Invalid trade fields")
        fill = next((row[key] for key in ("trade_id", "fill_id", "log_index")
                     if row.get(key) is not None), None)
        identity = [tx, condition, asset, side, str(price.normalize()),
                    str(size.normalize()), fill]
        uid = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        return cls(uid, int(stamp), condition, asset, side, price, size)


def get_json(url, **kwargs):
    response = requests.get(url, timeout=20, **kwargs)
    response.raise_for_status()
    return response.json(parse_float=Decimal)


def fetch(floor, get=get_json):
    params = {"user": WALLET, "limit": 100}
    cursors, trades = set(), []
    previous = None
    for _ in range(300):
        payload = get(TRADES, params=params.copy())
        rows, page = payload["data"], payload["pagination"]
        cursor = page["next_cursor"]
        if (not isinstance(rows, list) or (not rows and cursor is not None)
                or ("has_more" in page and page["has_more"] != (cursor is not None))):
            raise ValueError("Inconsistent trade page")
        for row in rows:
            trade = Trade.parse(row)
            if previous is not None and trade.timestamp > previous:
                raise ValueError("Trade pages are out of order")
            previous = trade.timestamp
            if previous < floor:
                return list(reversed(trades))
            trades.append(trade)
        if cursor is None:
            return list(reversed(trades))
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ValueError("Invalid or repeated trade cursor")
        cursors.add(cursor)
        params["cursor"] = cursor
    raise ValueError("Trade page limit reached before checkpoint")


def connect(path):
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER);
        CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY, cash TEXT);
        CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, ts INTEGER, asset TEXT,
            condition_id TEXT, side TEXT, price TEXT, size TEXT, treatment TEXT);
        CREATE TABLE IF NOT EXISTS source (asset TEXT PRIMARY KEY, shares TEXT);
        CREATE TABLE IF NOT EXISTS holdings (name TEXT, asset TEXT, condition_id TEXT,
            shares TEXT, cost TEXT, proxy TEXT, PRIMARY KEY(name, asset));
        CREATE TABLE IF NOT EXISTS ledger (name TEXT, id TEXT, kind TEXT, asset TEXT,
            shares TEXT, cash_delta TEXT, realized TEXT, PRIMARY KEY(name,id));
        CREATE TABLE IF NOT EXISTS resolved (condition_id TEXT PRIMARY KEY, winner TEXT);
    """)
    return db


def setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def save(db, key, value):
    db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))


def record(db, name, uid, kind, asset, quantity, delta, realized):
    cash = Decimal(db.execute("SELECT cash FROM accounts WHERE name=?", (name,)).fetchone()[0])
    db.execute("UPDATE accounts SET cash=? WHERE name=?", (str(cash + delta), name))
    db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)",
               (name, uid, kind, asset, str(quantity), str(delta), str(realized)))


def apply(db, trade):
    source = db.execute("SELECT shares FROM source WHERE asset=?", (trade.asset,)).fetchone()
    before = Decimal(source[0]) if source else ZERO
    buy = trade.side == "BUY"
    after = before + trade.size if buy else max(ZERO, before - trade.size)
    fraction = min(Decimal(1), trade.size / before) if not buy and before else ZERO
    db.execute("INSERT OR REPLACE INTO source VALUES (?,?)", (trade.asset, str(after)))
    for name, cash in db.execute("SELECT name,cash FROM accounts ORDER BY name").fetchall():
        row = db.execute("SELECT shares,cost FROM holdings WHERE name=? AND asset=?",
                         (name, trade.asset)).fetchone()
        shares, cost = map(Decimal, row) if row else (ZERO, ZERO)
        if buy:
            spend = trade.size * trade.price / 1000 if name == "A" else Decimal("10")
            if spend > Decimal(cash):
                continue
            quantity, delta, released = spend / trade.price, -spend, ZERO
            shares += quantity
            cost += spend
        elif shares and fraction:
            quantity = shares * fraction
            released = cost * fraction
            delta = quantity * trade.price
            shares -= quantity
            cost -= released
        else:
            continue
        record(db, name, trade.id, trade.side, trade.asset, quantity, delta,
               ZERO if buy else delta - released)
        if shares:
            db.execute("INSERT OR REPLACE INTO holdings VALUES (?,?,?,?,?,?)",
                       (name, trade.asset, trade.condition, str(shares), str(cost), str(trade.price)))
        else:
            db.execute("DELETE FROM holdings WHERE name=? AND asset=?", (name, trade.asset))


def ingest(db, now, get=get_json):
    baseline = setting(db, "baseline")
    if baseline is None:
        # Probe the live feed before initializing. Start at the time of this request.
        get(TRADES, params={"user": WALLET, "limit": 2})["pagination"]["next_cursor"]
        with db:
            db.executemany("INSERT INTO accounts VALUES (?,?)", [("A", str(START)), ("B", str(START))])
            for key in ("baseline", "checkpoint", "last_report"):
                save(db, key, now)
        return "baseline initialized"
    checkpoint = setting(db, "checkpoint")
    cutoff = now - DELAY
    if cutoff <= checkpoint:
        return "waiting for indexing delay"
    rows = fetch(max(baseline + 1, checkpoint - OVERLAP), get)
    count = 0
    with db:
        for trade in rows:
            if trade.timestamp > cutoff:
                continue
            if db.execute("SELECT 1 FROM trades WHERE id=?", (trade.id,)).fetchone():
                continue
            if trade.timestamp <= checkpoint:
                raise ValueError(f"Late unseen trade at {trade.timestamp}; checkpoint {checkpoint}")
            closed = db.execute("SELECT 1 FROM resolved WHERE condition_id=?",
                                (trade.condition,)).fetchone()
            if closed:
                raise ValueError(f"Trade arrived after settlement for {trade.condition}")
            db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)",
                       (trade.id, trade.timestamp, trade.asset, trade.condition, trade.side,
                        str(trade.price), str(trade.size), "observed"))
            apply(db, trade)
            db.execute("UPDATE holdings SET proxy=? WHERE asset=?", (str(trade.price), trade.asset))
            count += 1
        save(db, "checkpoint", cutoff)
    pending = {trade.condition for trade in rows if trade.timestamp > cutoff}
    return f"feed checked; {count} new trades", pending


def settlement_winner(market, condition):
    if market.get("condition_id") != condition or market.get("closed") is not True:
        return None
    tokens = market["tokens"]
    if len(tokens) != 2 or any(type(token.get("winner")) is not bool for token in tokens):
        return None
    winners = [str(token["token_id"]) for token in tokens if token["winner"]]
    return winners[0] if len(winners) == 1 else None


def settle(db, market, condition):
    winner = settlement_winner(market, condition)
    if winner is None:
        return
    assets = {str(token["token_id"]) for token in market["tokens"]}
    held = {row[0] for row in db.execute(
        "SELECT DISTINCT asset FROM holdings WHERE condition_id=?", (condition,))}
    if len(assets) != 2 or not held <= assets:
        raise ValueError("Resolution tokens do not match holdings")
    with db:
        if not db.execute("INSERT OR IGNORE INTO resolved VALUES (?,?)",
                          (condition, winner)).rowcount:
            return
        for name, asset, shares, cost in db.execute(
                "SELECT name,asset,shares,cost FROM holdings WHERE condition_id=?", (condition,)).fetchall():
            shares, cost = Decimal(shares), Decimal(cost)
            payout = shares if asset == winner else ZERO
            record(db, name, f"settle:{condition}:{asset}", "SETTLE", asset, shares, payout, payout - cost)
        db.execute("DELETE FROM holdings WHERE condition_id=?", (condition,))


def report(db):
    started = datetime.fromtimestamp(setting(db, "baseline"), timezone.utc)
    observed = db.execute("SELECT count(*) FROM trades").fetchone()[0]
    lines = [f"A/B paper only | since {started.isoformat()} | {observed} observed trades"]
    for name, cash in db.execute("SELECT name,cash FROM accounts ORDER BY name"):
        cash = Decimal(cash)
        holdings = db.execute("SELECT shares,cost,proxy FROM holdings WHERE name=?", (name,)).fetchall()
        cost = sum((Decimal(row[1]) for row in holdings), ZERO)
        proxy = sum((Decimal(shares) * Decimal(mark) for shares, _, mark in holdings), ZERO)
        realized = cash + cost - START
        counts = dict(db.execute("SELECT kind,count(*) FROM ledger WHERE name=? GROUP BY kind", (name,)))
        lines.append(f"{name} | cash ${cash:.2f} | realized paper P&L ${realized:+.2f} | "
                     f"{len(holdings)} open at cost ${cost:.2f} | open proxy value ${proxy:.2f} | "
                     f"buys {counts.get('BUY',0)}, sells {counts.get('SELL',0)}, settlements {counts.get('SETTLE',0)}")
    return "\n".join(lines)


def notify(message):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat or token.startswith("${{") or chat.startswith("${{"):
        return False
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat, "text": message}, timeout=15)
        return response.ok and response.json().get("ok") is True
    except (requests.RequestException, ValueError):
        return False


def main():
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if os.getenv("RAILWAY_ENVIRONMENT_ID") and (mount != "/data" or not os.path.ismount(mount)):
        raise RuntimeError("A/B requires its own mounted Railway volume at /data")
    db = connect(os.path.join(mount or ".", "breakthebank_ab.sqlite3"))
    next_settle = 0
    while True:
        now = int(time.time())
        try:
            state = ingest(db, now)
            print(state[0] if isinstance(state, tuple) else state, flush=True)
            if isinstance(state, tuple) and now >= next_settle:
                conditions = db.execute("SELECT DISTINCT condition_id FROM holdings").fetchall()
                # Check only when the condition has no trades pending through the safety delay.
                for (condition,) in conditions:
                    if condition not in state[1]:
                        try:
                            settle(db, get_json(MARKETS + condition), condition)
                        except (requests.RequestException, ValueError, KeyError):
                            print(f"Settlement lookup unavailable for {condition}", flush=True)
                next_settle = now + 900
            if state == "baseline initialized" or now - setting(db, "last_report") >= 86400:
                message = report(db)
                print(message, flush=True)
                print("Telegram digest delivered" if notify(message) else "Telegram digest unavailable", flush=True)
                with db:
                    save(db, "last_report", now)
        except (requests.RequestException, ValueError, KeyError, ArithmeticError) as exc:
            print(f"A/B ingestion or data error: {exc}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
