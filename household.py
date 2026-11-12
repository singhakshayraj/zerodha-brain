"""Household book — both accounts as one portfolio for buy targets (P-53).

Operator decisions 2026-11-12: sector caps, rebalance only through moves,
duplicates allowed while small, gold/silver ETFs a separate hedge sleeve.

Only *where freed money goes* changes; HOLD/SELL/TRIM verdicts are untouched.
For each rotation the target must pass, on the combined book of both
accounts (plus buys already suggested earlier in the same run):
  - its sector group stays <= SECTOR_CAP_PCT of the book after the buy
  - if already held anywhere, the combined position stays <= DUP_CAP_PCT
Among passing targets the existing rule still applies (same sector first,
then best score), except that a cross-sector pick prefers sectors below
their Nifty-500 weight. ETFs are never rotation targets, so the 15% hedge
sleeve cap needs no rule here — it is reported, not enforced.
"""
import json

import config
import sector_view

SECTOR_CAP_PCT = 20.0
DUP_CAP_PCT = 3.0
SINGLE_NAME_CAP_PCT = 8.0
HEDGE_SLEEVE_CAP_PCT = 15.0
DEPLOY = 0.95                      # same as advisor_rotation.ROTATION_DEPLOY_FRACTION
NOT_SECTORS = {'Outside Nifty 500', 'ETFs (gold, silver, index)'}   # buckets, not sectors: no sector cap


class Guard:
    """Running household state for one advisor run."""

    def __init__(self, positions: dict, sector_of: dict, index_weight: dict):
        self.value = {s: float(v) for s, v in positions.items() if v}
        self.sector_of = sector_of
        self.index_weight = index_weight or {}
        self.total = sum(self.value.values()) or 1.0

    def group(self, sym: str) -> str:
        return sector_view.group_of(sym, self.sector_of.get(sym))

    def group_pct(self, g: str, extra: float = 0.0) -> float:
        v = sum(val for s, val in self.value.items() if self.group(s) == g)
        return (v + extra) / self.total * 100

    def check(self, sym: str, buy_inr: float, sold_sym: str = None, sold_inr: float = 0.0):
        """(ok, reason) for buying buy_inr of sym, funded by selling sold_inr
        of sold_sym (a same-sector swap must not count the sold holding)."""
        g = self.group(sym)
        freed_here = sold_inr if sold_sym and self.group(sold_sym) == g else 0.0
        held = self.value.get(sym, 0.0)
        capped = g not in NOT_SECTORS
        if held and (held + buy_inr) / self.total * 100 > DUP_CAP_PCT:
            return False, (f"{sym} already {held / self.total * 100:.1f}% across accounts, "
                           f"{(held + buy_inr) / self.total * 100:.1f}% after this buy (cap {DUP_CAP_PCT:.0f}%)")
        if (held + buy_inr) / self.total * 100 > SINGLE_NAME_CAP_PCT:
            return False, f"{sym} would exceed {SINGLE_NAME_CAP_PCT:.0f}%"
        after = self.group_pct(g, buy_inr - freed_here)
        if capped and after > SECTOR_CAP_PCT:
            return False, f"{g} would be {after:.0f}% of the household (cap {SECTOR_CAP_PCT:.0f}%)"
        return True, None

    def underweight(self, sym: str) -> bool:
        g = self.group(sym)
        return self.group_pct(g) < (self.index_weight.get(g) or 0.0)

    def commit(self, sold_sym: str, sold_inr: float, bought_sym: str, buy_inr: float):
        """Apply a suggested rotation so later rows in the run see it."""
        if sold_sym in self.value:
            self.value[sold_sym] = max(0.0, self.value[sold_sym] - sold_inr)
        self.value[bought_sym] = self.value.get(bought_sym, 0.0) + buy_inr


def pick(exit_score, sector: str, scored: dict, guard: Guard, buy_inr: float,
         min_gap: int = None, min_target: int = None, sold_sym: str = None):
    """Household-aware rotation target, or None. Same gate as
    advisor_rotation.find_rotation_candidate; returns (target, note)."""
    min_gap = config.ROTATION_MIN_GAP if min_gap is None else min_gap
    min_target = config.ROTATION_MIN_TARGET_SCORE if min_target is None else min_target
    if exit_score is None or exit_score > config.ROTATION_MAX_EXIT_SCORE:
        return None, None
    ranked = sorted((c for c in scored.values()
                     if c['score'] >= min_target and c['score'] - exit_score >= min_gap),
                    key=lambda c: c['score'], reverse=True)
    skipped = []
    ok = []
    for c in ranked:
        if c.get('sector') and not guard.sector_of.get(c['symbol']):
            guard.sector_of[c['symbol']] = c['sector']
        good, why = guard.check(c['symbol'], buy_inr, sold_sym, buy_inr / DEPLOY)
        if good:
            ok.append(c)
        elif len(skipped) < 3:
            skipped.append(f"skipped {c['symbol']}: {why}")
    same = [c for c in ok if sector and c.get('sector') == sector]
    if same:
        c, reason = same[0], 'same_sector'
    elif ok:
        c = sorted(ok, key=lambda c: (guard.underweight(c['symbol']), c['score']), reverse=True)[0]
        reason = 'cross_sector'
    else:
        return None, '; '.join(skipped) or None
    target = {'symbol': c['symbol'], 'score': c['score'], 'sector': c.get('sector'),
              'reason': reason, 'last_close': c.get('last_close'), 'weekly': c.get('weekly')}
    note = '; '.join(skipped) if skipped and ranked and ranked[0]['symbol'] != c['symbol'] else None
    if reason == 'cross_sector' and guard.underweight(c['symbol']):
        uw = f"{guard.group(c['symbol'])} is underweight in the household"
        note = f"{note}; {uw}" if note else uw
    return target, note


def buy_estimate(row: dict) -> float:
    """Freed capital a rotation would redeploy (TRIM = half)."""
    q = float(row.get('quantity') or 0) * (0.5 if row.get('verdict') == 'TRIM' else 1)
    return q * float(row.get('last_price') or 0) * DEPLOY


# ── I/O ──────────────────────────────────────────────────────────────────────

def _index_weights() -> dict:
    import fundamentals
    by = (fundamentals.load_store() or {}).get('by_symbol') or {}
    g = {}
    for u in config.NIFTY500_UNIVERSE:
        m = (by.get(u['symbol']) or {}).get('mcap')
        if m:
            grp = sector_view.group_of(u['symbol'], u.get('sector'))
            g[grp] = g.get(grp, 0.0) + m
    tot = sum(g.values()) or 1.0
    return {k: v / tot * 100 for k, v in g.items()}


def add_rows(pos: dict, rows: list, pending: bool) -> dict:
    """Pure: add holdings value per symbol; when `pending`, also add the
    buy each row's rotation suggests (so two accounts are not both told to
    buy the same name on the same day)."""
    for r in rows:
        pos[r['symbol']] = pos.get(r['symbol'], 0.0) + float(r.get('quantity') or 0) * float(r.get('last_price') or 0)
        t = r.get('rotation_target_symbol')
        if pending and t and r.get('rotation_buy_qty') and r.get('rotation_buy_price'):
            pos[t] = pos.get(t, 0.0) + float(r['rotation_buy_qty']) * float(r['rotation_buy_price'])
    return pos


def other_accounts_positions(account: str, today: str = None) -> dict:
    """{symbol: value} of every account except `account`, from the latest
    stored advice (primary: last official run; previews: blob), plus that
    account's same-day suggested buys."""
    import account_preview
    import database as db
    from datetime import datetime
    import pytz
    today = today or datetime.now(pytz.timezone('Asia/Kolkata')).date().isoformat()
    pos = {}
    if account != 'primary':
        last = (db.supabase.table('portfolio_advice').select('run_id,run_date')
                .eq('is_official', True).order('created_at', desc=True).limit(1).execute().data)
        if last:
            rows = (db.supabase.table('portfolio_advice')
                    .select('symbol,quantity,last_price,rotation_target_symbol,rotation_buy_qty,rotation_buy_price')
                    .eq('run_id', last[0]['run_id']).execute().data or [])
            add_rows(pos, rows, pending=str(last[0]['run_date']) == today)
    for acct in account_preview.PREVIEW_ACCOUNTS:
        if acct == account:
            continue
        raw = db.get_config(account_preview.result_key(acct))
        blob = json.loads(raw) if raw else {}
        add_rows(pos, blob.get('rows') or [], pending=blob.get('run_date') == today)
    return pos


def guard_for(account: str, rows: list) -> Guard:
    """Guard over this run's rows + every other account's latest book."""
    pos = other_accounts_positions(account)
    for r in rows:
        pos[r['symbol']] = pos.get(r['symbol'], 0.0) + float(r.get('quantity') or 0) * float(r.get('last_price') or 0)
    sector_of = {u['symbol']: u.get('sector') for u in config.NIFTY500_UNIVERSE}
    try:
        iw = _index_weights()
    except Exception:
        iw = {}
    return Guard(pos, sector_of, iw)
