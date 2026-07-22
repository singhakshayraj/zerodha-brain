#!/usr/bin/env python3
"""Smoke test for backtest.py against real archived candles — NOT a gate #6
backtest (one day, no regime-period coverage). Proves the harness runs
end-to-end on real market data without crashing / miscounting, ahead of
having actual 2020-2022 historical data (blocked on the Kite Connect
historical API subscription, VISION §3c — external/account decision).

Usage: python3 scripts/backtest_smoke.py INFY 2026-07-22
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
from backtest import run_backtest


def _resample(candles_5min, bars_per_candle):
    out = []
    for i in range(0, len(candles_5min) - bars_per_candle + 1, bars_per_candle):
        chunk = candles_5min[i:i + bars_per_candle]
        out.append({
            'timestamp': chunk[0]['ts'],
            'open': chunk[0]['open'], 'high': max(c['high'] for c in chunk),
            'low': min(c['low'] for c in chunk), 'close': chunk[-1]['close'],
            'volume': sum(c.get('volume') or 0 for c in chunk),
        })
    return out


def main(symbol, run_date):
    candles = db.get_candles_for_symbol_from(symbol, f'{run_date}T00:00:00', run_date)
    if not candles:
        print(f"no candles for {symbol} on {run_date}")
        return
    # backtest.py reads 'timestamp'; the raw table uses 'ts'.
    candles_5min = [dict(c, timestamp=c['ts']) for c in candles]
    candles_15min = _resample(candles, 3)
    candles_1h = _resample(candles, 12)
    print(f"{symbol} {run_date}: {len(candles_5min)} 5m bars, "
          f"{len(candles_15min)} 15m, {len(candles_1h)} 1h (resampled)")

    for archetype in ('CONFLUENCE', 'ORB'):
        result = run_backtest(
            symbol_days={symbol: {run_date: {
                '5minute': candles_5min, '15minute': candles_15min, '60minute': candles_1h}}},
            nifty_days={run_date: {'5minute': []}},
            entry_archetype=archetype, exit_style='FIXED_TARGET',
        )
        print(f"  [{archetype}] n={result['n']} pf={result['profit_factor']} "
              f"win_rate={result['win_rate']} pnl={result['total_pnl']} "
              f"max_dd={result['max_drawdown']}")
        for t in result['trades']:
            print(f"    {t['direction']} entry={t['entry_price']} "
                  f"exit={t['exit_price']}({t['exit_reason']}) "
                  f"pnl={t['pnl']} R={t['r_multiple']}")


if __name__ == '__main__':
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
