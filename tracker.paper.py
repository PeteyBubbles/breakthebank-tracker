import os, time, json, sqlite3, datetime, hashlib, requests
from decimal import Decimal, InvalidOperation

WALLET = '0xf0318c32136c2db7fec88b84869aee6a1106c80c'
API = 'https://data-api.polymarket.com/v2/trades'
POLL = max(2, int(os.getenv('POLL_SECONDS', '2')))
BOT = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT = os.getenv('TELEGRAM_CHAT_ID', '')
DB_PATH = os.getenv('DB_PATH', '/data/trades.sqlite3')
START_CASH = Decimal('100')
BUY_USD = Decimal(os.getenv('PAPER_BUY_USD', '5'))
MAX_MARKET = Decimal(os.getenv('PAPER_MAX_MARKET_USD', '10'))
DURATION_HOURS = int(os.getenv('PAPER_HOURS', '72'))
SUMMARY_HOURS = int(os.getenv('SUMMARY_HOURS', '12'))
UTC = datetime.timezone.utc

def now(): return datetime.datetime.now(UTC)
def dec(v):
    try: return Decimal(str(v))
    except (InvalidOperation, TypeError): return Decimal('0')
def notify(message):
    print(message, flush=True)
    if BOT and CHAT:
        try:
            r = requests.post(f'https://api.telegram.org/bot{BOT}/sendMessage', json={'chat_id': CHAT, 'text': message}, timeout=15)
            r.raise_for_status()
        except requests.RequestException as exc: print('TELEGRAM ERROR:', exc, flush=True)

def fetch():
    r = requests.get(API, params={'user': WALLET, 'limit': 100}, timeout=15)
    r.raise_for_status()
    obj = r.json()
    if isinstance(obj, list): return obj
    if isinstance(obj, dict) and isinstance(obj.get('data', obj.get('trades', [])), list):
        return obj.get('data', obj.get('trades', []))
    raise ValueError('Unexpected trade API response')

def trade_key(t):
    # Include fill attributes: a transaction hash can cover more than one fill.
    fields = {k:t.get(k) for k in ('transactionHash','id','asset','conditionId','side','price','size','timestamp','outcome','logIndex')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()

def market_key(t): return str(t.get('asset') or (str(t.get('conditionId','')) + ':' + str(t.get('outcome',''))))

def init():
    parent = os.path.dirname(DB_PATH)
    if parent: os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute('create table if not exists seen (id text primary key, payload text, detected_at text)')
    db.execute('create table if not exists meta (key text primary key, value text)')
    db.execute('create table if not exists positions (asset text primary key, market text, outcome text, shares text, cost text, last_price text)')
    db.execute('create table if not exists fills (id text primary key, detected_at text, side text, asset text, market text, outcome text, source_price text, shares text, cash_delta text, note text)')
    db.commit()
    return db

def getmeta(db, k, default=None):
    row = db.execute('select value from meta where key=?',(k,)).fetchone()
    return row[0] if row else default

def setmeta(db, k, value): db.execute('insert or replace into meta values (?,?)',(k,str(value)))

def cash(db): return dec(getmeta(db, 'cash', str(START_CASH)))

def portfolio(db):
    positions = db.execute('select shares,last_price from positions').fetchall()
    marked = sum((dec(sh)*dec(p) for sh,p in positions), Decimal('0'))
    return cash(db), marked, cash(db)+marked

def summary(db):
    c, marked, equity = portfolio(db)
    count = db.execute('select count(*) from fills where shares != ?', ('0',)).fetchone()[0]
    elapsed = (now()-datetime.datetime.fromisoformat(getmeta(db,'started_at'))).total_seconds()/3600
    return (f'PAPER PORTFOLIO ({elapsed:.1f}h / {DURATION_HOURS}h)\n'
            f'Cash: ${c:.2f} | Marked positions: ${marked:.2f}\n'
            f'Estimated equity: ${equity:.2f} | P/L: ${equity-START_CASH:+.2f}\n'
            f'Simulated fills: {count}\n'
            'Valuation uses last observed source trade prices, NOT executable quotes. Fees/slippage excluded.')

def apply_trade(db, t, uid):
    side = str(t.get('side','')).upper()
    price = dec(t.get('price'))
    asset = market_key(t)
    title = str(t.get('title') or t.get('conditionId') or 'Unknown market')
    outcome = str(t.get('outcome') or '?')
    if not asset or price <= 0 or price >= 1 or side not in ('BUY','SELL'):
        return 'Skipped: unsupported side/price/asset'
    row = db.execute('select shares,cost,last_price from positions where asset=?',(asset,)).fetchone()
    shares, cost = (dec(row[0]),dec(row[1])) if row else (Decimal('0'),Decimal('0'))
    # This is a proxy mark, not an order-book quote.
    if row: db.execute('update positions set last_price=? where asset=?',(str(price),asset))
    simulated_shares = Decimal('0'); delta = Decimal('0'); note = ''
    if side == 'BUY':
        available = min(BUY_USD, MAX_MARKET-cost, cash(db))
        if available > 0:
            simulated_shares = available/price
            shares += simulated_shares; cost += available; delta = -available
        else: note = 'Skipped: cash or market exposure limit'
    else:
        # Do not short; sell at most existing shares, scaled to the source sale size.
        source_size = dec(t.get('size'))
        simulated_shares = min(shares, max(Decimal('0'),source_size))
        if simulated_shares > 0:
            avg_cost = cost/shares
            shares -= simulated_shares; cost -= avg_cost*simulated_shares
            delta = simulated_shares*price
        else: note = 'Skipped: no simulated shares to sell'
    if shares <= Decimal('0.000000001'):
        db.execute('delete from positions where asset=?',(asset,))
    else:
        db.execute('insert or replace into positions values (?,?,?,?,?,?)',
                   (asset,title,outcome,str(shares),str(max(cost,Decimal('0'))),str(price)))
    if delta:
        setmeta(db,'cash',cash(db)+delta)
    db.execute('insert or ignore into fills values (?,?,?,?,?,?,?,?,?,?)',
               (uid,now().isoformat(),side,asset,title,outcome,str(price),str(simulated_shares),str(delta),note))
    if delta:
        return f'PAPER {side}: {outcome} | {title}\nSource price: {price} | simulated cash change: ${delta:+.2f}\nCash left: ${cash(db):.2f}\nProxy fill only; not a real order.'
    return f'PAPER {side}: {outcome} | {title}\n{note}'

def main():
    if BUY_USD <= 0 or MAX_MARKET <= 0: raise ValueError('Paper buy amount and cap must be positive')
    db = init()
    notify('BreakTheBank paper tracker started (NO REAL ORDERS).')
    while True:
        try:
            rows = fetch()
            # First run only establishes a baseline, to avoid trading stale history.
            if getmeta(db,'baseline_complete') != 'yes':
                for t in rows:
                    db.execute('insert or ignore into seen values (?,?,?)',(trade_key(t),json.dumps(t),now().isoformat()))
                setmeta(db,'baseline_complete','yes')
                setmeta(db,'started_at',now().isoformat())
                setmeta(db,'cash',START_CASH)
                setmeta(db,'last_summary',now().isoformat())
                db.commit()
                notify(f'Paper baseline ready: {len(rows)} recent trades ignored. Starting $100 virtual cash; {DURATION_HOURS}h clock begins now.')
            else:
                started = datetime.datetime.fromisoformat(getmeta(db,'started_at'))
                active = (now()-started).total_seconds() < DURATION_HOURS*3600
                # Process oldest first. Only newly observed records are simulated.
                for t in reversed(rows):
                    uid = trade_key(t)
                    cur = db.execute('insert or ignore into seen values (?,?,?)',(uid,json.dumps(t),now().isoformat()))
                    if cur.rowcount and active:
                        msg = apply_trade(db,t,uid)
                        db.commit()
                        notify(msg)
                db.commit()
                last = datetime.datetime.fromisoformat(getmeta(db,'last_summary'))
                if (now()-last).total_seconds() >= SUMMARY_HOURS*3600:
                    notify(summary(db)); setmeta(db,'last_summary',now().isoformat()); db.commit()
                if not active and getmeta(db,'finished') != 'yes':
                    notify('72-HOUR PAPER EXPERIMENT COMPLETE\n'+summary(db))
                    setmeta(db,'finished','yes'); db.commit()
        except Exception as exc:
            print('ERROR:',repr(exc),flush=True)
        time.sleep(POLL)

if __name__ == '__main__': main()
