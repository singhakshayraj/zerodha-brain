#!/usr/bin/env bash
# One-command brain deploy — push to GitHub, which is what actually deploys.
#
# WHY (2026-08-05 incident): this service AUTO-DEPLOYS from GitHub
# (`railway status` → repo: singhakshayraj/zerodha-brain). The old script ran
# `railway up` (a local-tarball deploy); that image was then silently superseded
# by a GitHub rebuild at origin/main, so three unpushed commits (P-05 + advisor
# paper-portfolio) ran nowhere and a whole session logged the OLD build
# (git_sha c689ed4). The ONLY reliable deploy is `git push origin main`:
# it triggers CI (test-brain.yml) + the auto-deploy, and the GitHub build ships
# .git so config._resolve_git_sha() stamps the real HEAD sha (no manual GIT_SHA).
#
# This script refuses to "succeed" unless the code is committed AND pushed, so a
# dirty/unpushed tree can never again masquerade as deployed.
#
# Usage: ./scripts/deploy.sh          (run from the brain repo root)
set -euo pipefail

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
SHA="$(git rev-parse --short=12 HEAD)"

if [ "$BRANCH" != "main" ]; then
  echo "ABORT: on branch '$BRANCH', not main. The service deploys origin/main only."
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "ABORT: uncommitted changes — they will NOT deploy. Commit first, then re-run."
  git status --short
  exit 1
fi

echo "==> Pushing $SHA to origin/main (this is the deploy) ..."
git push origin main

echo ""
echo "==> Pushed. GitHub now builds + auto-deploys, gated by CI (test-brain.yml)."
echo "    Watch the build:   railway deployment list        # newest = SUCCESS when live"
echo "    Confirm it's live: latest brain_decisions.indicators.git_sha == $SHA"
echo ""
echo "==> Restart behaviour (the deploy restarts the brain):"
echo "    • Mid-session deploy  → brain AUTO-RESUMES the active RUNNING session."
echo "      Confirm: heartbeat status RUNNING + git_sha flips to $SHA on new decisions."
echo "    • No active session + market open (>=09:15 IST, trading day) → set"
echo "      brain_status=START for a fresh session (autopilot is suppressed once a"
echo "      session has already run today)."
echo "    • NEVER START before 09:15 — creates a dead MARKET_CLOSED session that"
echo "      blocks autopilot for the rest of the day."
