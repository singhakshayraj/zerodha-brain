"""Measure EXACTLY when a Zerodha enctoken dies. One-off experiment, 2026-08-10.

Why. Four places in this codebase state the enctoken expires "~06:00 IST", and
`TOKEN_REFRESH_HOUR_IST = 6:30` is built on that number — but it is an empirical
guess, not documentation. Zerodha documents only the Kite Connect `access_token`;
`enctoken` is the web-session cookie and is documented nowhere. Community
reports for access_token put the daily flush anywhere in **05:00–07:30 IST**,
which is wider than 06:00 and would mean 06:30 sits *inside* the window — a
refresh that fetches a token already doomed.

What this does. Polls `/user/profile` with the stored enctoken every
POLL_SECONDS and logs OK/DEAD with a timestamp, so the transition is captured to
the minute. Re-reads the token from `app_config` each poll, so pasting a fresh
one mid-run is picked up rather than ending the experiment.

It also settles a second question for free: **the probe is activity.** If the
expiry were an idle timeout, this traffic would keep the session alive. If the
token dies on schedule anyway, that confirms it is a server-side daily flush and
that no keep-alive scheme can work.

Run it detached so it survives a closed terminal:

    cd ~/Desktop/GITHUB/zerodha-brain
    nohup python3 scripts/probe_token_expiry.py > /dev/null 2>&1 &

Read the result:

    cat scripts/token_expiry_probe.log

Read-only: one GET per poll, no orders, no writes to prod.
"""
import os
import sys
import time
from datetime import datetime

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db                                  # noqa: E402
from kite_client import KiteClient, TokenExpiredError  # noqa: E402

IST = pytz.timezone('Asia/Kolkata')
POLL_SECONDS = 300          # 5 min — fine enough to bracket the flush
STOP_HOUR_IST = 9           # stop at 09:00, before the open
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   'token_expiry_probe.log')


def log(line: str) -> None:
    stamp = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')
    msg = f"{stamp}  {line}"
    print(msg, flush=True)
    with open(LOG, 'a') as f:
        f.write(msg + '\n')


def probe() -> str:
    """OK / DEAD / NO_TOKEN / INCONCLUSIVE.

    Mirrors scheduler._token_is_live: only a hard TokenExpiredError counts as
    death. A network blip must not be recorded as an expiry — that would be the
    one way this experiment could produce a confidently wrong answer.
    """
    token = db.get_enc_token()
    if not token:
        return 'NO_TOKEN'
    try:
        KiteClient(token).get_profile()
        return 'OK'
    except TokenExpiredError:
        return 'DEAD'
    except Exception as e:
        return f'INCONCLUSIVE ({type(e).__name__}: {e})'


def main() -> int:
    log(f"=== probe start · every {POLL_SECONDS}s until {STOP_HOUR_IST:02d}:00 IST ===")
    last = None
    first_ok = None
    while True:
        now = datetime.now(IST)
        if now.hour >= STOP_HOUR_IST:
            log(f"=== stop ({STOP_HOUR_IST:02d}:00 reached) · last state {last} ===")
            return 0

        state = probe()
        head = state.split(' ')[0]

        if head != (last or '').split(' ')[0]:
            log(f"STATE CHANGE -> {state}")
            if head == 'DEAD' and first_ok:
                alive = (now - first_ok).total_seconds() / 3600
                log(f">>> TOKEN DIED at {now.strftime('%H:%M IST')} "
                    f"(alive {alive:.1f}h since first OK at "
                    f"{first_ok.strftime('%H:%M IST')})")
                log(">>> Activity did NOT keep it alive — the probe was hitting "
                    "the API throughout. Confirms a scheduled flush, not an "
                    "idle timeout.")
                log(">>> Compare against TOKEN_REFRESH_HOUR_IST (06:30) and the "
                    "'~06:00' comments in config.py / token_refresher.py / "
                    "data_jobs.py.")
                return 0
        else:
            log(state)

        if head == 'OK' and first_ok is None:
            first_ok = now
        last = state
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    raise SystemExit(main())
