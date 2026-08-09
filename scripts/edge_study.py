"""[P-35] Walk-forward re-run of the entry-edge study.

Why this exists. Everything else is settled: [P-29]/[P-30] showed no exit policy
rescues the book, and none clears breakeven even at ZERO transaction cost. So
the edge, if there is one, has to be in the ENTRIES. [P-21] asked that question
in 2026-08-06 and answered "no" — but it named its own limitation:

    "Coverage is the key limitation: only 2 days carry SHORT labels
     (07-22, 07-23)... everything below is an in-sample candidate,
     not a validated edge."

The sample has since grown ~3.8x and now carries SHORT labels on TEN days.
Nobody re-ran it. This does, with the discipline the small sample previously
made impossible.

What a "decision label" is, and its two biases. decision_outcomes walks every
directional decision forward through the 5-min candle archive using its own
logged stop/target, whether or not it became a real trade. So:
  + it is ~9x larger than the taken-trade book and free of the pacing caps'
    selection effect (which trades got through is not random)
  - it is GROSS: it ignores slippage and costs entirely, unlike
    trades.r_multiple which bakes them in. A positive gross R is NOT a profit.
    This script therefore charges every decision its own exact cost in R.
  - same-bar stop+target ambiguity resolves stop-first (conservative).

Method.
  1. Expanding-window walk-forward. For each day d, derive the best rule using
     ONLY days before d, then score it on d. That produces a genuine
     out-of-sample track record rather than one arbitrary split — which is what
     [P-21] could not do with two days.
  2. Separately re-test [P-21]'s frozen rule (SHORT + before 13:00 + STRONG
     trend). It was derived on 07-22/23, so every later day is honest OOS.
  3. Report per-day, not just pooled. A rule that only works pooled is a rule
     that works on one big day.

Run:  python3 scripts/edge_study.py
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
ROUND_TRIP_COST = 0.0012          # 0.12%, the same assumption /autopsy uses
MIN_BUCKET = 40                   # below this, a bucket mean is noise


# ── data ─────────────────────────────────────────────────────────────────────

def load() -> list:
    """decision_outcomes ⋈ brain_decisions, usable rows only."""
    outs = []
    for frm in range(0, 100000, 1000):
        res = (db.supabase.table('decision_outcomes')
               .select('decision_id, run_date, direction, entry_price, '
                       'stop_price, r_multiple, exit_reason, bars_used')
               .not_.is_('r_multiple', 'null')
               .range(frm, frm + 999).execute())
        batch = res.data or []
        outs.extend(batch)
        if len(batch) < 1000:
            break

    decs = {}
    for frm in range(0, 100000, 1000):
        res = (db.supabase.table('brain_decisions')
               .select('id, symbol, decided_at, confidence_score, signal, indicators')
               .range(frm, frm + 999).execute())
        batch = res.data or []
        for d in batch:
            decs[d['id']] = d
        if len(batch) < 1000:
            break

    rows = []
    for o in outs:
        d = decs.get(o['decision_id'])
        if not d:
            continue
        ind = d.get('indicators') or {}
        entry, stop = o.get('entry_price'), o.get('stop_price')
        try:
            entry, stop = float(entry), float(stop)
            risk = abs(entry - stop)
            if risk <= 0:
                continue
        except (TypeError, ValueError):
            continue

        # Exact per-decision cost in R. Quantity cancels: both the cost and the
        # risk scale with it. This is strictly better than charging a flat
        # average, because cost-in-R depends on how wide that trade's stop was.
        cost_r = (ROUND_TRIP_COST * entry) / risk

        ts = d.get('decided_at')
        try:
            hour = datetime.fromisoformat(str(ts).replace('Z', '+00:00')).astimezone(IST).hour
        except Exception:
            continue

        rows.append({
            'day': str(o['run_date'])[:10],
            'dir': o.get('direction'),
            'gross_r': float(o['r_multiple']),
            'net_r': float(o['r_multiple']) - cost_r,
            'cost_r': cost_r,
            'hour': hour,
            'conf': d.get('confidence_score'),
            'adx': ind.get('adx'),
            'rsi': ind.get('rsi_14'),
            'trend_strength': ind.get('trend_strength'),
            'market_bias': ind.get('market_bias'),
            'regime': (ind.get('regime') or {}).get('regime') if isinstance(ind.get('regime'), dict) else ind.get('regime'),
            'candle_dir': ind.get('candle_direction'),
        })
    return rows


# ── stats ────────────────────────────────────────────────────────────────────

def stat(vals: list) -> dict:
    n = len(vals)
    if n == 0:
        return {'n': 0, 'mean': 0.0, 'se': 0.0, 't': 0.0}
    m = sum(vals) / n
    if n < 2:
        return {'n': n, 'mean': m, 'se': 0.0, 't': 0.0}
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    se = (var / n) ** 0.5
    return {'n': n, 'mean': m, 'se': se, 't': (m / se if se > 0 else 0.0)}


def fmt(s: dict, key='mean') -> str:
    return f"{s[key]:+.3f}R ±{s['se']:.3f} (n={s['n']}, t={s['t']:+.1f})"


# ── candidate rules ──────────────────────────────────────────────────────────
# Deliberately a SMALL, fixed menu. Searching a large space over ~6k rows finds
# a "winner" by construction — that is exactly how [P-21]'s in-sample rule
# appeared. Every candidate here is a hypothesis someone can state in words.

def rules():
    out = {
        'ALL (no filter)': lambda r: True,
        'SHORT only': lambda r: r['dir'] == 'SHORT',
        'LONG only': lambda r: r['dir'] == 'LONG',
        'morning (<13h)': lambda r: r['hour'] < 13,
        'afternoon (>=13h)': lambda r: r['hour'] >= 13,
        'STRONG trend': lambda r: r['trend_strength'] == 'STRONG',
        'ADX >= 25': lambda r: isinstance(r['adx'], (int, float)) and r['adx'] >= 25,
        'confidence >= 70': lambda r: isinstance(r['conf'], (int, float)) and r['conf'] >= 70,
        # [P-21]'s derived rule, frozen exactly as published
        'P21: SHORT+morning+STRONG': lambda r: (
            r['dir'] == 'SHORT' and r['hour'] < 13 and r['trend_strength'] == 'STRONG'),
        'SHORT + morning': lambda r: r['dir'] == 'SHORT' and r['hour'] < 13,
        'SHORT + STRONG': lambda r: r['dir'] == 'SHORT' and r['trend_strength'] == 'STRONG',
    }
    return out


def main():
    rows = load()
    days = sorted({r['day'] for r in rows})
    print(f"\n{'='*74}\n[P-35] ENTRY-EDGE WALK-FORWARD  ·  {len(rows)} labeled decisions "
          f"over {len(days)} days\n{'='*74}")
    print(f"days: {', '.join(days)}")
    g, n_ = stat([r['gross_r'] for r in rows]), stat([r['net_r'] for r in rows])
    c = sum(r['cost_r'] for r in rows) / len(rows)
    print(f"\npooled GROSS {fmt(g)}")
    print(f"pooled NET   {fmt(n_)}   (avg cost {c:.3f}R/decision @ {ROUND_TRIP_COST*100:.2f}%)")
    print("NOTE: gross ignores costs entirely. Net is the number that matters.")

    R = rules()

    # ── 1. pooled, net, per rule ────────────────────────────────────────────
    print(f"\n{'-'*74}\n1. POOLED (net of cost) — in-sample, shown only for orientation\n{'-'*74}")
    for name, f in R.items():
        s = stat([r['net_r'] for r in rows if f(r)])
        if s['n'] >= MIN_BUCKET:
            print(f"  {name:<32} {fmt(s)}")

    # ── 2. per-day consistency of the P-21 rule ─────────────────────────────
    print(f"\n{'-'*74}\n2. [P-21]'s FROZEN RULE, day by day (derived on 07-22/23; "
          f"later days are true OOS)\n{'-'*74}")
    p21 = R['P21: SHORT+morning+STRONG']
    pos = neg = 0

    for d in days:
        sub = [r['net_r'] for r in rows if r['day'] == d and p21(r)]
        s = stat(sub)
        if s['n'] == 0:
            print(f"  {d}   (no qualifying decisions)")
            continue
        tag = 'in-sample' if d <= '2026-07-23' else 'OOS'
        if d > '2026-07-23':
            pos += 1 if s['mean'] > 0 else 0
            neg += 1 if s['mean'] <= 0 else 0
        print(f"  {d}  {fmt(s):<44} {tag}")
    print(f"\n  Out-of-sample days positive: {pos}/{pos+neg}")

    # ── 3. expanding-window walk-forward ────────────────────────────────────
    print(f"\n{'-'*74}\n3. EXPANDING-WINDOW WALK-FORWARD — pick the best rule on all "
          f"PRIOR days,\n   then score it on the next day. The honest test.\n{'-'*74}")
    oos_all, picks = [], []
    for i, d in enumerate(days):
        if i == 0:
            continue
        train = [r for r in rows if r['day'] < d]
        best, best_m = None, None
        for name, f in R.items():
            if name == 'ALL (no filter)':
                continue
            s = stat([r['net_r'] for r in train if f(r)])
            if s['n'] >= MIN_BUCKET and (best_m is None or s['mean'] > best_m):
                best, best_m = name, s['mean']
        if not best:
            continue
        test = [r['net_r'] for r in rows if r['day'] == d and R[best](r)]
        s = stat(test)
        base = stat([r['net_r'] for r in rows if r['day'] == d])
        oos_all.extend(test)
        picks.append((d, best, s, base))
        print(f"  {d}  picked {best:<28} -> {fmt(s):<40} (day baseline {base['mean']:+.3f}R)")

    o = stat(oos_all)
    print(f"\n  POOLED OUT-OF-SAMPLE (net): {fmt(o)}")
    beat = sum(1 for _, _, s, b in picks if s['n'] and s['mean'] > b['mean'])
    print(f"  Days the chosen rule beat that day's own baseline: {beat}/{len(picks)}")

    # ── 3b. the two confounds that would explain this away ──────────────────
    # Any apparent entry edge here has two boring explanations. Both must be
    # ruled out explicitly or the result means nothing.
    print(f"\n{'-'*74}\n3b. CONFOUND CHECKS\n{'-'*74}")

    print("\n  (a) Is it just picking cheaper (wider-stop) trades?")
    print(f"      {'rule':<30}{'GROSS':>9}{'cost_R':>9}{'NET':>9}")
    for name, f in R.items():
        sub = [r for r in rows if f(r)]
        if len(sub) < MIN_BUCKET:
            continue
        gg = stat([r['gross_r'] for r in sub])
        nn = stat([r['net_r'] for r in sub])
        cc = sum(r['cost_r'] for r in sub) / len(sub)
        print(f"      {name:<30}{gg['mean']:>+9.3f}{cc:>9.3f}{nn['mean']:>+9.3f}")
    print("      Flat cost across rules => not a cost artefact; the edge is in GROSS.")

    print("\n  (b) Is it just shorting a falling market?")
    print("      Plain SHORT is the pure directional bet. If the rule only")
    print("      matches it, there is no entry skill on top of direction.")
    beat_short = 0
    days_cmp = 0
    for d in days:
        ru = stat([r['net_r'] for r in rows if r['day'] == d and p21(r)])
        sh = stat([r['net_r'] for r in rows if r['day'] == d and r['dir'] == 'SHORT'])
        if ru['n'] < 20 or sh['n'] < 20:
            continue
        days_cmp += 1
        beat_short += 1 if ru['mean'] > sh['mean'] else 0
        print(f"      {d}  rule {ru['mean']:+.3f}R  vs  SHORT {sh['mean']:+.3f}R  "
              f"({ru['mean'] - sh['mean']:+.3f})")
    print(f"      Rule beat plain SHORT on {beat_short}/{days_cmp} days.")

    # ── 4. verdict ──────────────────────────────────────────────────────────
    print(f"\n{'='*74}\nVERDICT\n{'='*74}")
    if o['n'] < MIN_BUCKET:
        print("  Insufficient out-of-sample data to judge.")
    elif o['mean'] > 0 and o['t'] > 2:
        print(f"  POSITIVE and distinguishable from noise ({fmt(o)}).")
        print("  This is an edge candidate worth a dark-flag deployment.")
    elif o['mean'] > 0:
        print(f"  Positive but NOT distinguishable from noise ({fmt(o)}).")
        print("  t < 2 — consistent with luck. Not actionable.")
    else:
        print(f"  NEGATIVE out-of-sample ({fmt(o)}).")
        print("  Rule selection on past days does not carry forward. No entry edge.")
    print()


if __name__ == '__main__':
    main()
