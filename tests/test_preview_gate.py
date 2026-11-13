"""Second-account preview waits for the day's universe scan (rotation source)."""
from datetime import datetime
from unittest.mock import patch

import pytz
import scheduler as sch

IST = pytz.timezone('Asia/Kolkata')


def _run(now, landed):
    started = []
    sch._preview_done.clear()
    with patch.object(sch, '_now_ist', return_value=IST.localize(now)), \
         patch.object(sch, '_scan_landed', return_value=landed), \
         patch.object(sch.db, 'get_config', return_value='tok'), \
         patch.object(sch, '_token_is_live', return_value=True), \
         patch.object(sch.threading, 'Thread') as th:
        th.return_value.start.side_effect = lambda: started.append(1)
        sch._maybe_run_account_previews()
    return bool(started)


def test_waits_for_scan_then_falls_back_at_deadline():
    assert _run(datetime(2026, 11, 12, 10, 0), landed=False) is False
    assert _run(datetime(2026, 11, 12, 10, 0), landed=True) is True
    assert _run(datetime(2026, 11, 12, 11, 31), landed=False) is True


def test_reruns_once_with_overwrite_when_scan_lands_after_fallback():
    """2026-11-13: tokens pasted 12:11, past the deadline -> preview ran before
    the scan with 0 targets. It must rerun once the scan lands, overwriting."""
    sch._preview_done.clear()
    calls = []

    def tick(landed):
        with patch.object(sch, '_now_ist', return_value=IST.localize(datetime(2026, 11, 13, 12, 13))), \
             patch.object(sch, '_scan_landed', return_value=landed), \
             patch.object(sch.db, 'get_config', return_value='tok'), \
             patch.object(sch, '_token_is_live', return_value=True), \
             patch.object(sch.threading, 'Thread') as th:
            th.side_effect = lambda target, **kw: type('T', (), {'start': lambda self: calls.append(target.__defaults__[2])})()
            sch._maybe_run_account_previews()

    tick(False); tick(False)          # fallback run once, not repeated
    tick(True); tick(True)            # one rerun after the scan, then done
    assert calls == [False, True]
