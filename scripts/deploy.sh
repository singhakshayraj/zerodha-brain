#!/usr/bin/env bash
# One-command brain deploy — uploads the working tree AND stamps GIT_SHA in the
# same step, so decisions are never stamped "unknown" again.
#
# WHY: deploys here are manual `railway up` (the service is not GitHub-connected)
# and the tarball does NOT include .git, so config._resolve_git_sha() falls back
# to the GIT_SHA env var. That var was hand-bumped as a separate step and once
# got forgotten -> 07-24 sessions stamped "unknown". This makes the two steps
# atomic. Run from the brain repo root; market closed (a restart is triggered).
#
# Usage: ./scripts/deploy.sh
set -euo pipefail

SERVICE="${RAILWAY_SERVICE:-zerodha-brain}"
SHA="$(git rev-parse --short=12 HEAD)"

if [ -n "$(git status --porcelain)" ]; then
  echo "WARNING: working tree is dirty — you are deploying uncommitted code."
  echo "         GIT_SHA will be stamped $SHA (the last commit), which will NOT"
  echo "         match what is actually running. Commit first for a truthful SHA."
  read -r -p "Continue anyway? [y/N] " ans
  [ "$ans" = "y" ] || { echo "Aborted."; exit 1; }
fi

echo "==> Deploying $SHA to service '$SERVICE' ..."
railway up --service "$SERVICE"

echo "==> Stamping GIT_SHA=$SHA (survives the tarball deploy) ..."
railway variables --set "GIT_SHA=$SHA" --service "$SERVICE"

echo "==> Done. Verify next session: trading_sessions.git_sha == $SHA"
echo ""
echo "==> Auto-start-on-build routine (a deploy restarts the brain):"
echo "    • Mid-session deploy  → the brain AUTO-RESUMES (reads brain_status=RUNNING,"
echo "      finds the active RUNNING session, resumes it). Confirm: heartbeat RUNNING."
echo "    • No active session + market open (>=09:15 IST, trading day) → set"
echo "      brain_status=START to start a fresh session (autopilot is suppressed"
echo "      once a session has already run today)."
echo "    • NEVER START before 09:15 — creates a dead MARKET_CLOSED session that"
echo "      blocks autopilot for the rest of the day."
