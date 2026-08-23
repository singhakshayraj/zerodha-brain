"""brain_activity retention must never run while a session could be live. [P-38]"""
from unittest.mock import MagicMock, patch

import scheduler


def _clock(hour, weekday=0):
    d = MagicMock()
    d.weekday.return_value = weekday
    d.hour = hour
    d.date.return_value.isoformat.return_value = '2026-08-24'
    return patch.object(scheduler, 'datetime',
                        MagicMock(now=MagicMock(return_value=d)))


def test_does_not_run_during_market_hours():
    """A delete competing with a live trading cycle is not worth the saving."""
    scheduler._activity_pruned_days.clear()
    with _clock(11), patch.object(scheduler.db, 'prune_activity') as p:
        scheduler._maybe_prune_activity()
    p.assert_not_called()


def test_runs_once_post_close():
    scheduler._activity_pruned_days.clear()
    with _clock(17), patch.object(scheduler.db, 'prune_activity', return_value=5) as p:
        scheduler._maybe_prune_activity()
        scheduler._maybe_prune_activity()
    assert p.call_count == 1


def test_never_raises_into_the_loop():
    scheduler._activity_pruned_days.clear()
    with _clock(18), patch.object(scheduler.db, 'prune_activity',
                                  side_effect=Exception('db down')):
        scheduler._maybe_prune_activity()   # must not raise
