# BreakTheBank public trade tracker

This is a read-only prototype. It **never places real or simulated orders**. It polls the public Polymarket Data API v2 and stores observed rows in SQLite. Optional Telegram alerts.

## Run

```bash
python -m pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='YOUR_BOT_TOKEN'
export TELEGRAM_CHAT_ID='YOUR_CHAT_ID'
python tracker.py
```

Telegram variables are optional; without them alerts print to the terminal.

The first poll establishes a baseline (no alerts for historical trades). The SQLite database persists deduplication across restarts.

## Important limitations

- The wallet address was supplied by the user and has not been independently verified as BreakTheBank's account.
- The public API can be delayed, paginate, rate-limit, or change response schemas; this simple latest-100 polling prototype cannot guarantee capture of every trade under high volume. The key construction is best-effort, not a guaranteed unique fill ID.
- For production use implement v2 cursor pagination, stable fill identifiers, feed-watermark monitoring, retries/backoff, orderbook simulation, and alerts on gaps.
- No live execution is included. Verify legal/platform eligibility before any future trading integration. Never circumvent geoblocking.

## A/B paper experiment

Run tests with `python -m unittest -v`. Run the independent worker with
`python -u tracker.ab.py`. On Railway it requires a dedicated volume mounted
at `/data` and writes `breakthebank_ab.sqlite3`. Keep the existing tracker and
its volume separate. The A/B branch can be deployed without merging into main.

| Rule | A | B |
| --- | --- | --- |
| Starting virtual cash | $1,000 | $1,000 |
| BUY allocation | Source shares × source price × 0.001 | $10 per eligible observed BUY |
| Insufficient virtual cash | Skip the whole BUY | Skip the whole BUY |
| SELL allocation | Sell the same fraction of open paper shares as estimated from observed source shares | Same |

A treats $1 million as a sizing assumption. The source's actual bankroll is
unknown. Both portfolios start empty on the same UTC cutoff, and only trades
after that second count. Sell fractions use post-launch observed source buys
and sells, including buys the paper portfolios skipped. Pre-launch inventory
and non-trade flows such as transfers, splits, merges and conversions are
unobserved. A sell with no observed inventory is skipped. A larger sell
liquidates the tracked cohort and may overestimate the actual fraction sold.

The worker reads the Polymarket Data API v2 through opaque cursors, rechecks
the previous hour, and delays posting trades by two minutes for indexing.
Same-second fills follow feed order, oldest first. Deduplication uses a
normalized transaction and fill fingerprint, including a fill ID or log
index when present. Indistinguishable fills can collapse when the feed omits a
fill identifier. A late unseen trade inside the overlap or an incomplete
batch blocks checkpoint advancement. Check Railway logs before using any
results after a feed incident. Delays beyond the overlap can be missed.

Paper exits use average cost. Binary settlement requires a closed market,
exactly one CLOB winner, and matching token IDs. The winning share pays $1
and the losing share pays $0. Ambiguous, unsupported or unavailable
resolutions stay open. Open values use the last observed source fill as a
possibly stale proxy. Realized paper P&L excludes open positions. All copies
assume source fill prices without modeling fees, slippage, queue position,
minimum order sizes, liquidity or follower latency. The experiment uses
public reads and sends no orders.

The worker logs a startup balance and a daily A/B digest. Set
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` on the A/B service to deliver those
same digests to Telegram. Source trades do not generate individual alerts.
For a first directional comparison, review at least 30 days and 30
independently settled markets, whichever takes longer. Group related markets
by event, check concentration and missing exits, and stress-test execution
costs before interpreting paper P&L as evidence of a tradable edge.
