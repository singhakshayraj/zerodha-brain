"""Structured logging to Axiom with stdout fallback.

Every log() call:
  1. Builds JSON event with session/cycle/symbol context
  2. Queues to background worker thread (non-blocking)
  3. Always prints to stdout (preserves Railway logs)

Set AXIOM_TOKEN + AXIOM_DATASET env vars to enable Axiom.
Without token: still prints to stdout, queue inert.
"""

import os
import threading
import queue
from datetime import datetime, timezone

AXIOM_TOKEN = os.environ.get('AXIOM_TOKEN', '')
AXIOM_DATASET = os.environ.get('AXIOM_DATASET', 'zerodha-trading')

_log_queue: queue.Queue = queue.Queue(maxsize=2000)
_context: dict = {}
_client = None
_client_init_attempted = False


def _get_client():
    global _client, _client_init_attempted
    if _client is not None or _client_init_attempted:
        return _client
    _client_init_attempted = True
    if not AXIOM_TOKEN:
        return None
    try:
        from axiom_py import Client
        _client = Client(AXIOM_TOKEN)
        print(f"[logger] Axiom client initialized dataset={AXIOM_DATASET}")
    except Exception as e:
        print(f"[logger] Axiom init failed: {e} — logging to stdout only")
        _client = None
    return _client


def _worker():
    """Background thread — drains queue and ships batches to Axiom."""
    while True:
        batch = []
        try:
            event = _log_queue.get(timeout=2.0)
            batch.append(event)
            while not _log_queue.empty() and len(batch) < 100:
                try:
                    batch.append(_log_queue.get_nowait())
                except queue.Empty:
                    break
        except queue.Empty:
            continue

        client = _get_client()
        if client and batch:
            try:
                client.ingest_events(dataset=AXIOM_DATASET, events=batch)
            except Exception:
                pass  # never block trading on logging


_worker_thread = threading.Thread(target=_worker, daemon=True)
_worker_thread.start()


def set_context(**kwargs):
    """Set fields added to every subsequent log."""
    _context.update(kwargs)


def clear_context():
    """Clear session context."""
    _context.clear()


def _log(level: str, tag: str, message: str, **data):
    """Core — non-blocking. Always prints to stdout."""
    event = {
        '_time': datetime.now(timezone.utc).isoformat(),
        'app': 'zerodha-brain',
        'level': level,
        'tag': tag,
        'message': message,
        **_context,
        **data,
    }
    try:
        _log_queue.put_nowait(event)
    except queue.Full:
        pass  # drop rather than block

    prefix = f"[{tag}]" if tag else ""
    print(f"{prefix} {message}".strip())


def info(message: str, tag: str = "", **data):
    _log("info", tag, message, **data)


def warning(message: str, tag: str = "", **data):
    _log("warning", tag, message, **data)


def error(message: str, tag: str = "", **data):
    _log("error", tag, message, **data)


def signal(symbol: str, action: str, confidence: int, regime: str, **data):
    """Structured signal event."""
    _log(
        "info", "signal",
        f"{action} {symbol} conf={confidence}% regime={regime}",
        symbol=symbol, action=action,
        confidence=confidence, regime=regime,
        **data,
    )


def trade(symbol: str, side: str, qty: int, price: float, **data):
    """Structured trade event."""
    _log(
        "info", "trade",
        f"{side} {symbol} x{qty} @ Rs{price:.2f}",
        symbol=symbol, side=side,
        qty=qty, price=price,
        **data,
    )


def cycle(cycle_num: int, stocks: int, **data):
    """Structured cycle start event."""
    _log(
        "info", "cycle",
        f"Cycle {cycle_num} — scanning {stocks} stocks",
        cycle=cycle_num, stocks=stocks,
        **data,
    )
