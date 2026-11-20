"""Strategy-lab runner [P-70]: ~54 pre-registered intraday variants paper-trade
the live Nifty 200 feed in parallel. Paper only — no Kite order endpoint is
ever called from here.

Loop (IST): every 5-min boundary + 30s from 09:30 to 15:10 fetch 5-min bars
once and fan them out to every variant (entries end 14:30 inside the
strategies). A retail enctoken has no /quote/ltp (400 on every call), so the
price is the latest bar's close and stops/targets are checked against each
new bar's open/high/low; flatten 15:15; stats 15:20. Yields Kite calls while the advisor or
the daily-bar sync runs. Restart-safe: today's OPEN rows reload from lab_trades
and the in-progress cycle is skipped, so a redeploy never duplicates entries.
"""
import time
from collections import Counter
from datetime import datetime, timedelta

import pytz

import config
import lab_db
from kite_client import TokenExpiredError
from lab_book import Book, Position
from lab_stats import cumulative, day_row
from lab_strategies import VARIANTS, Ctx, _t, completed_bars, prior_close, registry_rows

IST = pytz.timezone('Asia/Kolkata')
NIFTY_TOKEN = 256265
_POS_FIELDS = ('strategy_id', 'symbol', 'side', 'qty', 'entry_price', 'entry_ts', 'stop',
               'target', 'slip_bps', 'risk_inr', 'entry_charges', 'entry_slip_inr', 'entry_ref')


def pick_universe(today: str, n: int = 200) -> list:
    """Top n of the Nifty 500 pin by 20-day average traded value."""
    tok = {r['symbol']: int(r['instrument_token']) for r in config.NIFTY500_UNIVERSE}
    adv = lab_db.adv(list(tok), today)
    ranked = sorted((s for s in tok if adv.get(s)), key=lambda s: -adv[s])[:n]
    return [(s, tok[s], i + 1) for i, s in enumerate(ranked)]


def _f(x):
    return None if x is None else float(x)


def _bar_start(bar) -> datetime:
    return _t(bar)


def _bar_hit(p, bar):
    """(reason, price) if this bar reaches the stop or target. Stop first when
    both sit inside one bar (conservative); a gap past the stop fills at the
    open and Book.close caps it at stop - 0.25R."""
    if p.side == 'LONG':
        if bar['open'] <= p.stop or bar['low'] <= p.stop:
            return 'STOP', min(bar['open'], p.stop)
        if p.target is not None and bar['high'] >= p.target:
            return 'TARGET', p.target
    else:
        if bar['open'] >= p.stop or bar['high'] >= p.stop:
            return 'STOP', max(bar['open'], p.stop)
        if p.target is not None and bar['low'] <= p.target:
            return 'TARGET', p.target
    return None


class LabRunner:
    def __init__(self, kite, today: str, should_yield, clock=lambda: datetime.now(IST),
                 universe=None, sleep=time.sleep):
        self.kite, self.today, self.should_yield = kite, today, should_yield
        self.clock, self.sleep = clock, sleep
        self.universe = universe if universe is not None else pick_universe(today, config.LAB_UNIVERSE_N)
        self.books = {v.id: Book(v.id) for v in VARIANTS}
        self.bars, self.nifty_bars, self.last_ltp = {}, [], {}
        self.rs_order = None
        self.refusals = Counter()
        self.flattened = False
        self._last_cycle = None

    # ── data ────────────────────────────────────────────────────────────────
    def _fetch(self, token, old):
        now = self.clock()
        start = (now - timedelta(days=15)) if not old else now
        got = self.kite.get_historical_data(
            token, '5minute', start.strftime('%Y-%m-%d 00:00:00'), now.strftime('%Y-%m-%d %H:%M:%S'))
        self.sleep(config.LAB_FETCH_DELAY_S)
        if not old:
            return got
        keep = [b for b in old if str(b['timestamp'])[:10] < self.today]
        return keep + [b for b in got if str(b['timestamp'])[:10] == self.today]

    def _price(self, sym):
        """Latest close incl. the in-progress bar — the only live price an
        enctoken offers (same fallback the old engine used)."""
        bars = self.bars.get(sym) or []
        today = [b for b in bars if str(b['timestamp'])[:10] == self.today]
        if today:
            self.last_ltp[sym] = today[-1]['close']
        return self.last_ltp.get(sym)

    def _bar_exits(self, sym):
        """Stops/targets against every bar after the entry bar (stateless: a
        bar that hit would already have closed the position last cycle)."""
        for b in self.books.values():
            for p in [p for p in b.open if p.symbol == sym]:
                ent = datetime.fromisoformat(str(p.entry_ts)).astimezone(IST).replace(tzinfo=None)
                first = ent.replace(minute=ent.minute - ent.minute % 5, second=0, microsecond=0) + timedelta(minutes=5)
                for bar in self.bars.get(sym) or []:
                    if _bar_start(bar) < first:
                        continue
                    hit = _bar_hit(p, bar)
                    if hit:
                        reason, px = hit
                        lab_db.close_trade(p.db_id, b.close(p, px, self.clock().isoformat(), reason))
                        break

    # ── lifecycle ───────────────────────────────────────────────────────────
    def resume(self):
        have = {p.db_id for b in self.books.values() for p in b.open}
        for r in lab_db.closed_on(self.today):
            b = self.books.get(r['strategy_id'])
            if b:
                b.entries_today[r['symbol']] = b.entries_today.get(r['symbol'], 0) + 1
        for r in lab_db.load_open(self.today):
            b = self.books.get(r['strategy_id'])
            if not b or r['id'] in have:
                continue
            kw = {k: r[k] for k in _POS_FIELDS}
            for k in ('qty',):
                kw[k] = int(kw[k])
            for k in ('entry_price', 'stop', 'target', 'slip_bps', 'risk_inr', 'entry_charges',
                      'entry_slip_inr', 'entry_ref'):
                kw[k] = _f(kw[k])
            b.open.append(Position(**kw, db_id=r['id']))
            b.entries_today[r['symbol']] = b.entries_today.get(r['symbol'], 0) + 1
        now = self.clock()
        self._last_cycle = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)

    def cycle(self) -> bool:
        """One decision pass. False = yielded to the advisor/bar sync (Kite is
        shared, ~3 req/s); the caller retries the same boundary next tick."""
        if self.should_yield():
            return False
        now = self.clock()
        for sym, token, _ in self.universe:
            if self.should_yield():
                return False
            self.bars[sym] = self._fetch(token, self.bars.get(sym))
        self.nifty_bars = self._fetch(NIFTY_TOKEN, self.nifty_bars)
        nifty = completed_bars(self.nifty_bars, now)
        if self.rs_order is None and (now.hour, now.minute) >= (10, 15):
            self.rs_order = self._rank_rs(now, nifty)
        for sym, _, rank in self.universe:
            self._bar_exits(sym)
            ltp = self._price(sym)
            if not ltp:
                continue
            ctx = Ctx(now, sym, completed_bars(self.bars.get(sym) or [], now), ltp, nifty, rank,
                      {'order': self.rs_order or []})
            try:
                for v in VARIANTS:
                    sig = v.signal(ctx)
                    if sig:
                        self._enter(v.id, sym, sig, ltp, 5 if rank <= 100 else 15, now)
            except TokenExpiredError:
                raise
            except Exception as e:
                print(f'[lab] {sym} signal error (skipped): {e}')
        return True

    def _rank_rs(self, now, nifty):
        npc = prior_close(nifty)
        nchg = (nifty[-1]['close'] / npc - 1) if npc and nifty else 0.0
        rs = {}
        for sym, _, _ in self.universe:
            cb = completed_bars(self.bars.get(sym) or [], now)
            pc = prior_close(cb)
            if pc and cb:
                rs[sym] = cb[-1]['close'] / pc - 1 - nchg
        return sorted(rs, key=lambda s: -rs[s])

    def _enter(self, sid, sym, sig, ltp, slip_bps, now):
        side, stop, target = sig['side'], sig['stop'], sig['target']
        ok = stop < ltp if side == 'LONG' else stop > ltp
        if target is not None:
            ok = ok and (target > ltp if side == 'LONG' else target < ltp)
        if not ok:
            self.refusals[(sid, 'bad_levels')] += 1
            return
        res = self.books[sid].try_open(sym, side, ltp, stop, target, slip_bps, self.clock().isoformat())
        if isinstance(res, str):
            self.refusals[(sid, res)] += 1
            return
        res.db_id = lab_db.insert_open(res, self.today)

    def flatten(self, reason: str):
        opened = [(b, p) for b in self.books.values() for p in b.open]
        if opened and reason != 'TOKEN_LOST':
            tok = {s: t for s, t, _ in self.universe}
            for sym in sorted({p.symbol for _, p in opened}):
                if sym in tok:
                    self.bars[sym] = self._fetch(tok[sym], self.bars.get(sym))
        ts = self.clock().isoformat()
        for b, p in opened:
            ltp = (self._price(p.symbol) if reason != 'TOKEN_LOST' else self.last_ltp.get(p.symbol)) or p.entry_ref
            lab_db.close_trade(p.db_id, b.close(p, ltp, ts, reason))
        self.flattened = True

    def finish_day(self):
        by = {}
        for t in lab_db.closed_on(self.today):
            by.setdefault(t['strategy_id'], []).append(
                {k: _f(t[k]) for k in ('net_inr', 'gross_inr', 'charges_inr', 'slippage_inr',
                                       'gross_r', 'cost_r', 'net_r')})
        status = lab_db.statuses()
        rows = []
        for sid, trades in by.items():
            d = day_row(trades)
            hist = [{k: v if k in ('trade_date', 'strategy_id', 'sprt_state') else _f(v) for k, v in h.items()}
                    for h in lab_db.daily_history(sid) if str(h['trade_date']) != self.today]
            c = cumulative(hist + [d], len(VARIANTS))
            if status.get(sid, 'running') == 'running' and c['sprt_state'] != 'running':
                lab_db.set_status(sid, c['sprt_state'])
            rows.append({'strategy_id': sid, **d, **c})
        lab_db.write_daily(self.today, rows)
        lab_db.bump_refusals(self.today, dict(self.refusals))

    def tick(self) -> bool:
        now = self.clock()
        hm = (now.hour, now.minute)
        if hm >= (15, 15):
            if not self.flattened:
                self.flatten('EOD')
            if hm >= (15, 20):
                self.finish_day()
                return False
            return True
        boundary = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
        if ((now - boundary).total_seconds() >= 30 and boundary != self._last_cycle
                and (9, 30) <= (boundary.hour, boundary.minute) <= (15, 10)):
            if self.cycle():
                self._last_cycle = boundary
        return True

    def run(self) -> str:
        lab_db.register(registry_rows())
        self.resume()
        try:
            while self.tick():
                self.sleep(10)
            return 'EOD'
        except TokenExpiredError:
            self.refusals[('ALL', 'token')] += 1
            self.flatten('TOKEN_LOST')
            self.finish_day()
            return 'TOKEN_LOST'
