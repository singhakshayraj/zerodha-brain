# Zerodha Brain

Python decision engine for the paper-trading validation system — the intraday
auto-trader **and** the daily portfolio advisor. Runs 24/7 on Railway, reads
commands + writes all state to Supabase, uses Zerodha Kite only for **read-only**
market data (paper mode places no real orders).

> **Canonical project docs live in the dashboard repo** (`zerodha-trading/docs/`):
> `STATUS.md` (where we are), `ROADMAP.md` (what's next), `VISION.md` (why + the
> go/no-go gates). Start there. This README is brain-repo operations only.

## Deploy

Manual (the service is not GitHub-connected — `railway up` ships a tarball):

```bash
./scripts/deploy.sh      # railway up + stamps GIT_SHA in one step
```

`scripts/deploy.sh` sets `GIT_SHA` so decisions aren't stamped `unknown` (the
tarball has no `.git`). CI (`.github/workflows/test-brain.yml`) runs the full
suite on py3.11 + a 70% coverage gate on every push.

## Layout

```
scheduler.py   → brain.py → signal_engine.py / trend_tells.py / orb.py
                          → risk_manager.py
                          → order_manager.py | paper_broker.py   (PAPER_TRADING)
                          → market_data.py → kite_client.py
                          → market_regime.py, inplay.py, indicators.py
                          → database.py → Supabase

Advisor (advisory-only, never touches an order path):
  portfolio_advisor.py  → advise() + the run_advisor / _lite / _timeline_capture loops
  advisor_risk.py       → portfolio_risk (concentration + return-correlation + tax-loss)
  advisor_digest.py     → Telegram digest + decision keyboard
  advisor_backtest.py   → grading, factor attribution, confidence calibration
  stock_agent.py        → per-stock 24/7 observation timeline
```

## Environment (Railway)

```
SUPABASE_URL=                 SUPABASE_SERVICE_KEY=
PAPER_TRADING=true            # paper mode — no real orders (see docs/PAPER_TRADING.md)
GIT_SHA=                      # set by scripts/deploy.sh
DATA_COLLECTION_MODE=true     # soft session stops -> counterfactuals (paper only)
ENFORCE_DAILY_STOP_3R=true    # ...except the -3R daily stop, always hard
ADVISOR_TELEGRAM_BOT_TOKEN=   ADVISOR_TELEGRAM_CHAT_ID=
```

## Run flow

Deploy → set env → in the dashboard paste the daily enc_token (before 09:15 IST)
→ START (or autopilot). The advisor runs itself ~09:20 IST on a live token,
independent of trading sessions.

## Tests

```bash
pytest -q          # 828 tests; pure-function core, mocked Supabase
```

Paper-trading mode: [docs/PAPER_TRADING.md](docs/PAPER_TRADING.md).
