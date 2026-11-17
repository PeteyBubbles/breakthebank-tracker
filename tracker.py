import os,time,json,sqlite3,datetime,requests
WALLET='0xf0318c32136c2db7fec88b84869aee6a1106c80c'
API='https://data-api.polymarket.com/v2/trades'
POLL=int(os.getenv('POLL_SECONDS','2'))
BOT=os.getenv('TELEGRAM_BOT_TOKEN','')
CHAT=os.getenv('TELEGRAM_CHAT_ID','')
db=sqlite3.connect('trades.sqlite3')
db.execute('create table if not exists seen (id text primary key, payload text, detected_at text)')
db.commit()
def notify(msg):
 print(msg,flush=True)
 if BOT and CHAT:
  r=requests.post(f'https://api.telegram.org/bot{BOT}/sendMessage',json={'chat_id':CHAT,'text':msg},timeout=15);r.raise_for_status()
def fetch():
 r=requests.get(API,params={'user':WALLET,'limit':100},timeout=15);r.raise_for_status()
 data=r.json()
 if isinstance(data,list):return data
 if isinstance(data,dict):return data.get('data',data.get('trades',[]))
 raise ValueError('Unexpected response shape')
def key(t):
 return str(t.get('id') or t.get('transactionHash') or t.get('transaction_hash') or json.dumps(t,sort_keys=True)) + ':' + str(t.get('logIndex',t.get('log_index','')))
def main():
 notify('BreakTheBank public trade monitor started (NO REAL ORDERS).')
 seeded=False
 while True:
  try:
   rows=fetch()
   if not isinstance(rows,list):raise ValueError('Unexpected trade list')
   fresh=[]
   for t in reversed(rows):
    k=key(t)
    cur=db.execute('insert or ignore into seen values (?,?,?)',(k,json.dumps(t),datetime.datetime.now(datetime.timezone.utc).isoformat()))
    if cur.rowcount and seeded:fresh.append(t)
   db.commit()
   for t in fresh:
    notify(f"BreakTheBank trade: {t.get('side','?')} {t.get('outcome','')} | {t.get('title',t.get('conditionId','market'))} | price {t.get('price','?')} | size {t.get('size','?')} | tx {t.get('transactionHash','?')}")
   if not seeded:print(f'Baseline initialized with {len(rows)} recent rows; no historical alerts.',flush=True)
   seeded=True
  except Exception as e:print('ERROR:',str(e),flush=True)
  time.sleep(POLL)
if __name__=='__main__':main()
