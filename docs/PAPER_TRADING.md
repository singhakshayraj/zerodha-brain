# Paper Trading Mode

Real market data + real decision pipeline + **simulated fills**. No Kite
orders are ever placed. Built for the month-long validation run planned in
`zerodha-trading/docs/PAPER_TRADING_ROADMAP.md` (dashboard repo).

## How it works

- `config.PAPER_TRADING` (env `PAPER_TRADING=true`) selects `PaperBroker`
  instead of `OrderManager` in `brain.py`. That is the ONLY behavioral change.
- `paper_broker.py` implements OrderManager's exact interface
  (`place_buy_order`, `place_sell_order`, `place_short_order`,
  `cover_short_order`, `square_off_all`). Fills happen at the **live LTP**
  (read-only `kite.get_ltp` quote call) with adverse slippage
  (`PAPER_SLIPPAGE_PCT`, default 0.05%).
- Order ids look like `PAPER-<hex12>`, so paper trades are identifiable in
  the `trades` table (`entry_order_id`/`exit_order_id`).
- If no live price is available the order fails (returns `None`) rather than
  inventing a fill — a fabricated price would poison the training dataset.
- `scheduler.py` writes `app_config.paper_mode = 'true'|'false'` at session
  start so the dashboard can label paper sessions.
- Signals, risk manager, regime detector, indicators, market data, and all
  Supabase writes (sessions/trades/decisions/heartbeat) run unchanged.

## Enabling on Railway

Set env vars on the Railway service:

```
PAPER_TRADING=true
PAPER_SLIPPAGE_PCT=0.05   # optional, default 0.05
```

Redeploy. Startup log prints `[BRAIN] PAPER TRADING mode — no real orders
will be placed`. Still requires a fresh daily enc_token (quotes are
authenticated), but the token is only ever used for read-only calls in this
mode plus profile/holdings reads.

## Safety notes

- `PaperBroker.place_sell_order` has no CNC safety lock — nothing real can be
  sold. The live-mode lock in `OrderManager` is untouched.
- Default remains real trading (`PAPER_TRADING` unset → false). Flip
  deliberately.
