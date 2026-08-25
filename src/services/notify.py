"""services/notify.py — Webhook notifier (SPEC.md §W3 T17 / invariant V12).

Sends Discord-compatible JSON payloads ({"content": "..."} — ntfy/Discord
webhook URLs both work). Delivery contract per V12:
  - exactly 3 attempts with exponential backoff (1s, 2s, 4s)
  - final failure is logged, NEVER raised into the caller/run

Gating (config):
  WEBHOOK_URL empty            -> everything disabled
  NOTIFY_ON = none             -> everything disabled
  NOTIFY_ON = failures         -> only notify_failure()
  NOTIFY_ON = all              -> summaries + failures
"""

from __future__ import annotations

import logging
import time

import requests

from src.core import config

logger = logging.getLogger(__name__)

_ATTEMPTS = 3
_BACKOFF_SECONDS = (1, 2, 4)


def _post(payload: dict) -> bool:
    """POST once-configured webhook with retries. Returns final success."""
    url = config.WEBHOOK_URL
    if not url:
        return False
    for attempt in range(_ATTEMPTS):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if 200 <= resp.status_code < 300:
                return True
            logger.warning(
                "Webhook attempt %d/%d returned HTTP %d",
                attempt + 1, _ATTEMPTS, resp.status_code,
            )
        except requests.RequestException as exc:
            logger.warning("Webhook attempt %d/%d failed: %s", attempt + 1, _ATTEMPTS, exc)
        except Exception as exc:  # §W3 V12: nothing may escape into the caller/run
            logger.warning("Webhook attempt %d/%d unexpected error: %s", attempt + 1, _ATTEMPTS, exc)
        if attempt < _ATTEMPTS - 1:
            time.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
    logger.error("Webhook delivery failed after %d attempts", _ATTEMPTS)
    return False


def _gated(kind: str) -> bool:
    """kind is 'summary' or 'failure'."""
    if not config.WEBHOOK_URL or config.NOTIFY_ON == "none":
        return False
    if kind == "summary":
        return config.NOTIFY_ON == "all"
    return True  # failures pass in both 'failures' and 'all' modes


def notify_run_summary(
    run_type: str,
    downloaded: int = 0,
    failed: int = 0,
    scraped: int = 0,
    requeued: int = 0,
    notes: str | None = None,
) -> bool:
    """Post-run summary (§W3 T17). Fired from _record_run_complete."""
    if not _gated("summary"):
        return False
    line = (
        f"musicstream {run_type} run — scraped {scraped}, "
        f"downloaded {downloaded}, failed {failed}, requeued {requeued}"
    )
    if notes:
        line += f" | {notes}"
    return _post({"content": line})


def notify_failure(title: str, detail: str = "") -> bool:
    """Immediate failure alert (token issues, corruption, auto-blocks)."""
    if not _gated("failure"):
        return False
    content = f"⚠️ musicstream: {title}"
    if detail:
        content += f"\n{detail}"
    return _post({"content": content})
