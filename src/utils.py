"""
src/utils.py — Shared utilities used across multiple modules.
"""

from __future__ import annotations

import hashlib

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
