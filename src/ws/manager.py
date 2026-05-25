"""WebSocket connection manager.

Concurrency-safe broadcast and connection tracking. Audit #15 fixes:
  - the original mutated active_connections during iteration via disconnect()
    when a send raised, with no lock — list could grow/shrink while iterating
  - stale connections were never pruned; sockets that errored stayed in the
    list forever, accumulating noise on every broadcast
  - .remove() on a missing connection raised ValueError silently (e.g. when
    the same socket disconnect-fired twice)

Fix: use an asyncio.Lock for membership mutation, snapshot the list before
broadcasting, and prune dead sockets after each broadcast pass.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            try:
                self.active_connections.remove(websocket)
            except ValueError:
                # Already removed (e.g. broadcast pruned it); safe to ignore.
                pass

    async def broadcast(self, message: str) -> None:
        # Snapshot under the lock so a concurrent connect/disconnect can't
        # mutate the list while we iterate.
        async with self._lock:
            snapshot = list(self.active_connections)

        dead: Set[WebSocket] = set()
        for connection in snapshot:
            try:
                await connection.send_text(message)
            except Exception as exc:  # noqa: BLE001
                # Mark the socket dead and log at debug (broadcast errors
                # spam the log on a normal client navigation).
                logger.debug("WebSocket send failed; pruning: %s", exc)
                dead.add(connection)

        if dead:
            async with self._lock:
                self.active_connections = [
                    c for c in self.active_connections if c not in dead
                ]


manager = ConnectionManager()
