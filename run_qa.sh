#!/usr/bin/env bash
# Off-hours QA rehearsal: run the REAL brain against the SIM Supabase project
# with a synthetic market (qa_market.FakeKiteClient). Full production code
# path — scheduler, signal engine, risk manager, paper broker — no Kite, no
# market-hours gating, zero prod writes.
#
# Required env (get both from the sim project's Supabase dashboard →
# Settings → API; sim project ref: fbfluafzxgynasvuryiu):
#   export SIM_SUPABASE_SERVICE_KEY=...
#
# Usage:  ./run_qa.sh
set -euo pipefail

: "${SIM_SUPABASE_SERVICE_KEY:?set SIM_SUPABASE_SERVICE_KEY (sim project service_role key)}"

export QA_MODE=true
export PAPER_TRADING=true
export SUPABASE_URL="https://fbfluafzxgynasvuryiu.supabase.co"
export SUPABASE_SERVICE_KEY="$SIM_SUPABASE_SERVICE_KEY"

echo "[QA] Brain starting against SIM Supabase (fbfluafzxgynasvuryiu)"
echo "[QA] QA_MODE=true PAPER_TRADING=true — synthetic market, paper fills"
exec python3 main.py
