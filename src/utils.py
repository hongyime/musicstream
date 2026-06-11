"""
src/utils.py — Shared utilities used across multiple modules.
"""

from __future__ import annotations

import hashlib
import logging
import socket
import time

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 65_536  # 64 KiB


def compute_sha256(path: str) -> str:
    """
    Return the SHA-256 hex digest of the file at *path*.

    Reads in 64 KiB chunks to avoid loading large audio files into memory.

    Raises:
        OSError: if the file cannot be opened or read.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def is_internet_available(host: str = "8.8.8.8", port: int = 53, timeout: float = 3.0) -> bool:
    """
    Check if internet connectivity is available by attempting to connect to a reliable host.
    Defaults to Google DNS (8.8.8.8:53).
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except (socket.error, Exception):
        return False


def wait_for_internet(quiet: bool = False) -> None:
    """
    Block until internet connectivity is restored.
    Logs a warning on the first failure and a success message when restored.
    """
    if is_internet_available():
        return

    if not quiet:
        logger.warning("Internet connectivity lost. Pausing and waiting for restoration...")

    waited = False
    while not is_internet_available():
        waited = True
        time.sleep(5)

    if waited and not quiet:
        logger.info("Internet connectivity restored. Resuming operations.")
