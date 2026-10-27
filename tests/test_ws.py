import pytest

from src.ws.manager import ConnectionManager


@pytest.mark.asyncio
async def test_disconnect_is_idempotent() -> None:
    manager = ConnectionManager()
    socket = object()
    manager.active_connections.append(socket)  # type: ignore[arg-type]

    await manager.disconnect(socket)  # type: ignore[arg-type]
    await manager.disconnect(socket)  # type: ignore[arg-type]

    assert manager.active_connections == []
