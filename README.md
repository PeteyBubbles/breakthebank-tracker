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
