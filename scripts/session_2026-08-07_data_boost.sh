#!/usr/bin/env bash
# Volume + diversity boost for the next session. Run PRE-MARKET (before 09:15
# IST) — `railway variables --set` restarts the brain, which would truncate a
# running session's data collection.
#
# Context — REVISED 2026-08-08 on 08-07's live evidence. The numbers below are
# current; do not "adjust before running" (an earlier STATUS note said to, and
# was stale).
#
# Originally measured on 2026-08-06 (session 16f23213, 77 trades, 1,883
# decisions across only 46 symbols): HOURLY_PACE 44 / CYCLE_LIMIT 13 /
# CONCURRENT_CAP 1 / SYMBOL_DAY_CAP 1.
#
# Then [P-31] roughly doubled the universe (46 -> 86 symbols), which moved the
# constraint, and 08-07 settled where it landed ([C4]): across 20 cycles the
# tally stayed at CYCLE_LIMIT 3 -- ALL of them in cycle 1's opening burst, none
# after -- while entries per IST hour ran 11 / 8 / 15, and 15 is exactly
# DATA_MAX_NEW_TRADES_PER_HOUR.
#
# So the hourly cap is what actually binds: 15 -> 25 is the lever that matters,
# and 8 -> 12 on the cycle cap is close to irrelevant (kept anyway -- it only
# widens the opening burst, and costs nothing).
#
# Caveat carried from [C1]: 08-07 was a 2h50m session because the enc_token was
# never pasted, so it is a thin read on pacing. The deeper constraint that day
# was session LENGTH, not pacing.
#
# The DIVERSITY half of this is already shipped in code (brain 18b34f9,
# DATA_UNIVERSE_ROTATION_N=40, sector-balanced Nifty 500 rotation) and needs no
# variable set — it engages by itself under data-collection mode.
#
# What follows is the VOLUME half, which needs these env vars.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Raising the caps that actually bound (leaving the two that don't)"
railway variables --service zerodha-brain \
  --set "DATA_MAX_NEW_TRADES_PER_HOUR=25" \
  --set "MAX_TRADES_PER_CYCLE=12" \
  --set "DATA_MAX_TRADES_PER_DAY=250"

# Deliberately NOT changed:
#   DATA_MAX_TRADES_PER_SYMBOL stays 6 — with ~86 symbols in play we want
#     breadth (new names), not depth (the same name six more times).
#   DATA_MAX_CONCURRENT_POSITIONS stays 20 — only 1 deferral. Raise it only if
#     CONCURRENT_CAP starts showing up in the deferral tally once the hourly
#     gate is loosened, since that is when it would begin to bind.

echo
echo "==> Confirm"
railway variables --service zerodha-brain \
  | grep -E "DATA_MAX|MAX_TRADES_PER_CYCLE|DATA_UNIVERSE_ROTATION_N|DATA_COLLECTION_MODE" || true

cat <<'NOTE'

Done. Two things to know before the open:

1. This restarts the brain. Fine now; never during a session.
2. More trades at ~-0.4R each means a LARGER paper loss. That is the intended
   trade: the daily stop is already a soft counterfactual
   (ENFORCE_DAILY_STOP_3R=false) and data collection is the goal. Do not read
   tomorrow's P&L as a strategy signal — the exit-frontier work already showed
   costs, not signal quality, drive the number.

Optional, needs a DB write this session could not make:
  Widen the universe axis further by switching the session from NIFTY50 to
  BOTH (adds Nifty Next 50, ~50 more names). Every session so far has run
  NIFTY50, so the Next-50 half of that axis has never been exercised:

    update app_config
       set value = '{"capitalDeployed":100000,"maxTrades":40,"maxLossPercent":5,
                     "maxProfitPercent":15,"tradeIntervalSeconds":300,
                     "stockUniverse":"BOTH","experimentCell":"BOTH_ROT40_PACE25"}'
     where key = 'session_config';

  Consider this OPTIONAL and probably a step too far for one day: stacked on
  top of the +40 rotation it would put ~136 symbols in the universe, and cycle
  time scales with universe size. Prefer one change at a time — check VERIFY
  V-6 (cycle cadence) after tomorrow, then decide.

After the close, run /post-session-check: V-5 and V-6 in docs/reference/VERIFY.md
are the two checks that say whether this worked.
NOTE
