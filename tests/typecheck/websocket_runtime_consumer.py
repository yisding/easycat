"""Static consumer contract for optional WebSocket session factories."""

from websockets.asyncio.server import ServerConnection

from easycat import Session
from easycat.server.transports import WebSocketSessionRuntime


def sync_optional_session(_connection: ServerConnection) -> Session | None:
    return None


async def async_optional_session(_connection: ServerConnection) -> Session | None:
    return None


def accept_optional_factories(manager: object) -> None:
    WebSocketSessionRuntime[ServerConnection, Session](
        manager=manager,
        max_sessions=1,
        session_factory=sync_optional_session,
    )
    WebSocketSessionRuntime[ServerConnection, Session](
        manager=manager,
        max_sessions=1,
        session_factory=async_optional_session,
    )
