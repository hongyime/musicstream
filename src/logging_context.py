"""Audit #33: per-track logging context.

Pattern:
    from src.logging_context import track_context
    with track_context(track_id=track.id):
        ... pipeline code ...

All log records emitted inside the `with` block carry track_id=N in the
`[%(track_id)s]` slot of the root formatter. Outside any context the slot
shows `-`, which is short enough not to bloat noisy logs.

Why a contextvar (not threadlocal): asyncio coroutines on the daemon
hop tasks freely; threadlocal would lose context across `await`. Python
3.7+ contextvars are propagated automatically by `asyncio.create_task`.
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Iterator, Optional

# Module-level so every importer shares the same var.
_track_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "musicstream_track_id", default=None
)


class TrackContextFilter(logging.Filter):
    """Inject track_id into every LogRecord. Always returns True (never filters out)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not hasattr(record, "track_id") or record.track_id in (None, ""):
            value = _track_id_var.get()
            record.track_id = value if value is not None else "-"
        return True


@contextmanager
def track_context(track_id: int) -> Iterator[None]:
    """Push track_id onto the current context for the duration of the block."""
    token = _track_id_var.set(int(track_id))
    try:
        yield
    finally:
        _track_id_var.reset(token)


def current_track_id() -> Optional[int]:
    """Read access for callers that want to embed the id in custom payloads."""
    return _track_id_var.get()
