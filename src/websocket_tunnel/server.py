"""Tunnel server: accepts control connections from tunnel clients."""

from __future__ import annotations

import asyncio
import logging
import ssl

import websockets
from websockets.asyncio.server import ServerConnection

from . import __version__
from .config import ConfigError, ServerConfig, split_host_port
from .protocol import MAX_CONTROL_SIZE
from .session import Session


class TunnelServer:
    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._log = logging.getLogger("wtunnel.server")
        self._sessions: set[Session] = set()
        self._stop_event = asyncio.Event()
        self.ready_event = asyncio.Event()
        self._conn_sem: asyncio.Semaphore | None = (
            asyncio.Semaphore(config.max_connections) if config.max_connections > 0 else None
        )

    async def run(self) -> None:
        ssl_context = self._build_ssl()
        host, port = split_host_port(self._config.listen)
        async with websockets.serve(
            self._handle_connection,
            host,
            port,
            ssl=ssl_context,
            max_size=MAX_CONTROL_SIZE,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=1,
            compression=None,
        ) as _:
            scheme = "wss" if ssl_context else "ws"
            self._log.info("wtunnel %s listening on %s:%s (%s)", __version__, host, port, scheme)
            try:
                await self._stop_event.wait()
            finally:
                await self._close_sessions()

    def _build_ssl(self) -> ssl.SSLContext | None:
        if not self._config.tls_cert:
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(self._config.tls_cert, self._config.tls_key)
        except (OSError, ssl.SSLError) as exc:
            raise ConfigError(f"failed to load TLS certificate: {exc}") from exc
        return context

    async def _handle_connection(self, ws: ServerConnection) -> None:
        if self._conn_sem is not None:
            if self._conn_sem.locked():
                self._log.warning("max_connections reached; rejecting new connection")
                await ws.close()
                return
            await self._conn_sem.acquire()
        session = Session(
            ws,
            role="server",
            own_name="server",
            own_proxies=self._config.proxies,
            token=self._config.token,
            allow_peer_backends=self._config.allow_peer_backends,
            allow_peer_listens=self._config.allow_peer_listens,
            ready_event=self.ready_event,
            logger=self._log,
        )
        self._sessions.add(session)
        try:
            await session.run()
        finally:
            self._sessions.discard(session)
            if self._conn_sem is not None:
                self._conn_sem.release()

    async def _close_sessions(self) -> None:
        sessions = list(self._sessions)
        for session in sessions:
            await session.close()
        self._sessions.clear()

    async def stop(self) -> None:
        self._stop_event.set()
        await self._close_sessions()
