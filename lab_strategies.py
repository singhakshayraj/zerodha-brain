"""Strategy-lab archetypes + the pre-registered variant grid [P-70].

Pure: each archetype maps a Ctx (completed 5-min bars, LTP, clock) to an entry
{side, stop, target} or None. 6 archetypes x (3 stop widths x 3 entry
thresholds) = 54 variants. A changed rule is a new id (_v2), never an edit —
lab_db.register refuses a changed spec_hash.
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import inplay
import orb
from indicators import calculate_atr

NO_ENTRY_AFTER = (14, 30)


def _t(bar) -> datetime:
    return datetime.strptime(str(bar['timestamp'])[:16], '%Y-%m-%dT%H:%M')


def _hm(now) -> tuple:
    return (now.hour, now.minute)


def completed_bars(candles_5min: list, now_ist) -> list:
    """Drop the in-progress bar: a bar starting at T is complete at T+5min."""
    now = now_ist.replace(tzinfo=None)
    return [b for b in candles_5min if _t(b) + timedelta(minutes=5) <= now]


def resample(candles_5min: list, minutes: int) -> list:
    out, key_of = [], None
    for b in candles_5min:
        t = _t(b)
        key = (t.date(), (t.hour * 60 + t.minute - 555) // minutes)
        if key != key_of:
            out.append(dict(b))
            key_of = key
        else:
            cur = out[-1]
            cur['high'] = max(cur['high'], b['high'])
            cur['low'] = min(cur['low'], b['low'])
            cur['close'] = b['close']
            cur['volume'] = (cur.get('volume') or 0) + (b.get('volume') or 0)
    return out


def _today(bars: list) -> list:
    if not bars:
        return []
    d = _t(bars[-1]).date()
    return [b for b in bars if _t(b).date() == d]


def prior_close(candles_5min: list):
    if not candles_5min:
        return None
    d = _t(candles_5min[-1]).date()
    prev = [b for b in candles_5min if _t(b).date() < d]
    return prev[-1]['close'] if prev else None


@dataclass
class Ctx:
    now_ist: datetime
    symbol: str
    bars: list                 # completed 5-min bars, prior days + today
    ltp: float
    nifty_bars: list
    rank_adv: int
    rs_rank: dict              # {'order': [symbols, strongest first]} from 10:15
    cache: dict = field(default_factory=dict)   # shared by variants of one symbol-cycle


def _in(now, start, end) -> bool:
    return start <= _hm(now) <= end


def _pct_stop(side, ltp, pct):
    return ltp * (1 - pct / 100) if side == 'LONG' else ltp * (1 + pct / 100)


def _scaled(side, ltp, natural_stop, mult):
    d = abs(ltp - natural_stop) * mult
    return ltp - d if side == 'LONG' else ltp + d


def _opening(ctx):
    """(prior close, today's first three bars) for the 09:30 gap archetypes."""
    t = _today(ctx.bars)
    if len(t) < 3 or _t(t[0]).strftime('%H:%M') != '09:15':
        return None, None
    return prior_close(ctx.bars), t[:3]


def _gapgo(ctx, stop_pct, gap_min):
    if not _in(ctx.now_ist, (9, 30), (9, 34)):
        return None
    pc, first = _opening(ctx)
    if not pc:
        return None
    gap = (first[0]['open'] - pc) / pc * 100
    if gap >= gap_min and min(b['low'] for b in first) > pc:
        side = 'LONG'
    elif gap <= -gap_min and max(b['high'] for b in first) < pc:
        side = 'SHORT'
    else:
        return None
    return {'side': side, 'stop': _pct_stop(side, ctx.ltp, stop_pct), 'target': None}


def _gapfade(ctx, stop_pct, gap_min):
    if not _in(ctx.now_ist, (9, 30), (9, 34)):
        return None
    pc, first = _opening(ctx)
    if not pc:
        return None
    gap = (first[0]['open'] - pc) / pc * 100
    if gap >= gap_min and first[2]['close'] < first[0]['open']:
        side = 'SHORT'
    elif gap <= -gap_min and first[2]['close'] > first[0]['open']:
        side = 'LONG'
    else:
        return None
    return {'side': side, 'stop': _pct_stop(side, ctx.ltp, stop_pct), 'target': pc}


def _vwaprev(ctx, stop_pct, k_atr):
    if ctx.rank_adv > 100 or not _in(ctx.now_ist, (9, 45), NO_ENTRY_AFTER):
        return None
    t = _today(ctx.bars)
    den = sum(b.get('volume') or 0 for b in t)
    if not den:
        return None
    vwap = sum((b['high'] + b['low'] + b['close']) / 3 * (b.get('volume') or 0) for b in t) / den
    atr = calculate_atr(ctx.bars[-50:])
    if not atr:
        return None
    dev = ctx.ltp - vwap
    if dev >= k_atr * atr:
        side = 'SHORT'
    elif dev <= -k_atr * atr:
        side = 'LONG'
    else:
        return None
    return {'side': side, 'stop': _pct_stop(side, ctx.ltp, stop_pct), 'target': vwap}


def _rshold(ctx, stop_pct, k):
    if not _in(ctx.now_ist, (10, 15), (10, 19)):
        return None
    order = (ctx.rs_rank or {}).get('order') or []
    k = int(k)
    if len(order) < 2 * k:
        return None
    if ctx.symbol in order[:k]:
        side = 'LONG'
    elif ctx.symbol in order[-k:]:
        side = 'SHORT'
    else:
        return None
    return {'side': side, 'stop': _pct_stop(side, ctx.ltp, stop_pct), 'target': None}


def _orb(ctx, mult, min_rvol):
    if not _in(ctx.now_ist, (9, 30), (11, 0)):
        return None
    if 'or_stats' not in ctx.cache:
        ctx.cache['or_stats'] = inplay.opening_range_stats(ctx.bars)
    st = ctx.cache['or_stats']
    if not st or st.get('or_rvol') is None or st['or_rvol'] < min_rvol:
        return None
    sig = orb.orb_signal(ctx.ltp, st)
    if sig.get('action') not in ('BUY', 'SELL'):
        return None
    side = 'LONG' if sig['action'] == 'BUY' else 'SHORT'
    return {'side': side, 'stop': _scaled(side, ctx.ltp, sig['stop_loss'], mult),
            'target': sig['target'] if mult == 1 else None}


def _nifty_change(nifty_bars):
    pc = prior_close(nifty_bars)
    if not pc or not nifty_bars:
        return 0.0
    return (nifty_bars[-1]['close'] - pc) / pc * 100


def _conf_signal(ctx):
    if 'conf' not in ctx.cache:
        from signal_engine import SignalEngine
        chg = _nifty_change(ctx.nifty_bars)
        direction = 'BULLISH' if chg >= 0.5 else 'BEARISH' if chg <= -0.5 else 'SIDEWAYS'
        ctx.cache['conf'] = SignalEngine().generate_signal(
            ctx.bars, resample(ctx.bars, 15), resample(ctx.bars, 60), ctx.ltp, ctx.symbol,
            direction, chg, now=ctx.now_ist)
    return ctx.cache['conf']


def _conf(ctx, mult, min_conf):
    if not _in(ctx.now_ist, (9, 30), NO_ENTRY_AFTER):
        return None
    sig = _conf_signal(ctx)
    if sig.get('action') not in ('BUY', 'SELL') or (sig.get('confidence') or 0) < min_conf:
        return None
    side = 'LONG' if sig['action'] == 'BUY' else 'SHORT'
    target = None
    if mult == 1 and sig.get('target'):
        reward = abs(sig['target'] - ctx.ltp)
        target = ctx.ltp + reward if side == 'LONG' else ctx.ltp - reward
    return {'side': side, 'stop': _scaled(side, ctx.ltp, sig['stop_loss'], mult), 'target': target}


# arch -> (fn, stop axis [(tag, value)], entry axis [loose, base, strict], rule text)
_PCT = [('s0.5', 0.5), ('s1', 1.0), ('s2', 2.0)]
_NAT = [('n1', 1), ('n2', 2), ('n4', 4)]
ARCHETYPES = {
    'CONF': (_conf, _NAT, [60, 70, 80],
             'signal_engine BUY/SELL on 15m/60m resample with confidence >= entry; '
             'ATR stop x stop; target only at x1; 09:30-14:30'),
    'ORB': (_orb, _NAT, [1.0, 1.5, 2.0],
            '15-min opening-range break with OR RVOL >= entry; stop far side x stop; '
            '1 measured-move target only at x1; 09:30-11:00'),
    'GAPGO': (_gapgo, _PCT, [1.0, 1.5, 2.5],
              'at 09:30, |gap| >= entry% and first 15m never trades back through prior close '
              '-> continue; stop %; exit stop or 15:15'),
    'GAPFADE': (_gapfade, _PCT, [1.0, 1.5, 2.5],
                'at 09:30, |gap| >= entry% and 09:25 close back inside day open -> fade; '
                'target prior close; stop %'),
    'VWAPREV': (_vwaprev, _PCT, [1.5, 2.0, 3.0],
                'ADV rank <= 100, |ltp - vwap| >= entry x ATR14(5m) -> revert; target VWAP; '
                'stop %; 09:45-14:30'),
    'RSHOLD': (_rshold, _PCT, [3, 5, 10],
               'at 10:15, top/bottom K of universe by %chg since prior close minus Nifty '
               '-> long/short; stop %; exit stop or 15:15'),
}
_ENTRY_TAGS = ('loose', 'base', 'strict')


@dataclass
class Variant:
    id: str
    arch: str
    stop_param: float
    entry_param: float

    def signal(self, ctx: Ctx):
        if _hm(ctx.now_ist) > NO_ENTRY_AFTER:
            return None
        return ARCHETYPES[self.arch][0](ctx, self.stop_param, self.entry_param)


VARIANTS = [Variant(f'{arch}_{st}_{et}_v1', arch, sv, ev)
            for arch, (_, stops, entries, _) in ARCHETYPES.items()
            for st, sv in stops
            for et, ev in zip(_ENTRY_TAGS, entries)]


def registry_rows() -> list:
    rows = []
    for v in VARIANTS:
        rule = ARCHETYPES[v.arch][3]
        spec = {'arch': v.arch, 'stop_param': v.stop_param, 'entry_param': v.entry_param, 'rule': rule}
        rows.append({'id': v.id, 'arch': v.arch,
                     'params': {'stop_param': v.stop_param, 'entry_param': v.entry_param},
                     'rule': rule,
                     'spec_hash': hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()})
    return rows
