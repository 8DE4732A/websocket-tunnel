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
from .session import STALE_CLOSE_TIMEOUT, Session


class TunnelServer:
    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._log = logging.getLogger("wtunnel.server")
        self._sessions: set[Session] = set()
        # peer instance id -> the session that currently owns it.  A reconnecting
        # client reuses its instance id, letting us evict a stale half-open
        # session immediately instead of waiting for the ping timeout.
        self._instances: dict[str, Session] = {}
        # Sessions that hold a max_connections slot.  Tracked as a set rather
        # than a Semaphore so that evicting a stale session can hand its slot
        # to the replacement deterministically.
        self._admitted: set[Session] = set()
        # Per-instance claim locks.  Keyed by instance id so that unrelated
        # clients reconnecting at the same time never wait for each other's
        # slow teardown.
        self._claim_locks: dict[str, asyncio.Lock] = {}
        # Connection handlers that have not finished yet, including sessions
        # still in the handshake.  Bounds unauthenticated connection floods
        # without blocking a reconnect behind its own stale session.
        self._active_handlers = 0
        self._stop_event = asyncio.Event()
        self.ready_event = asyncio.Event()

    # Extra admission headroom for connections that have not authenticated
    # yet.  A reconnect needs one transient slot while its stale session is
    # being torn down, so a hard cap of exactly max_connections would reject
    # it.  The headroom still bounds an unauthenticated flood.
    _HANDSHAKE_HEADROOM = 8

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
        limit = self._config.max_connections
        if limit > 0 and self._active_handlers >= limit + self._HANDSHAKE_HEADROOM:
            self._log.warning("connection limit reached (unauthenticated flood?)")
            await ws.close()
            return
        self._active_handlers += 1

        async def admit(session: Session) -> None:
            """Enforce max_connections once the peer identity is known.

            Doing this after the handshake lets a reconnect evict its own
            stale session first, so it is not rejected by the slot that
            session still holds.
            """
            if session.peer_instance and self._instances.get(session.peer_instance) is not session:
                # A concurrent claim replaced us; do not start serving.
                raise ConnectionError("superseded by a newer connection")
            if session in self._admitted:
                # The slot was handed over by _claim_instance.
                return
            if limit <= 0:
                return
            if len(self._admitted) >= limit:
                raise ConnectionError(f"max_connections ({limit}) reached")
            self._admitted.add(session)

        session: Session | None = None
        try:
            session = Session(
                ws,
                role="server",
                own_name="server",
                own_proxies=self._config.proxies,
                token=self._config.token,
                allow_peer_backends=self._config.allow_peer_backends,
                allow_peer_listens=self._config.allow_peer_listens,
                on_peer_instance=self._claim_instance,
                on_authenticated=admit,
                ready_event=self.ready_event,
                logger=self._log,
            )
            self._sessions.add(session)
            await session.run()
        finally:
            if session is not None:
                self._sessions.discard(session)
                self._admitted.discard(session)
                instance = session.peer_instance
                if instance:
                    if self._instances.get(instance) is session:
                        del self._instances[instance]
                    # Drop the per-instance lock once nobody owns the id.
                    lock = self._claim_locks.get(instance)
                    if lock is not None and not lock.locked() and instance not in self._instances:
                        self._claim_locks.pop(instance, None)
            self._active_handlers -= 1

    def _claim_lock(self, instance: str) -> asyncio.Lock:
        """Return the lock serialising claims for one instance id."""
        lock = self._claim_locks.get(instance)
        if lock is None:
            lock = asyncio.Lock()
            self._claim_locks[instance] = lock
        return lock

    async def _claim_instance(self, instance: str, session: Session) -> None:
        """Evict a stale session when the same client reconnects.

        A client that lost connectivity reconnects within a second, but this
        node may not notice the old socket is dead until the WebSocket ping
        times out (~40s).  Without eviction the new session cannot bind the
        same listener ports.  Matching on the process-level instance id keeps
        this safe: genuinely different clients never evict each other.

        The lock is per instance, so a slow teardown of one client's stale
        session never delays another client's reconnect.
        """
        async with self._claim_lock(instance):
            old = self._instances.get(instance)
            if old is not None and old is not session:
                self._log.info("peer %s reconnected; closing stale session", instance[:8])
                # Hand the max_connections slot straight to the replacement so
                # no unrelated connection can take it first.
                if old in self._admitted:
                    self._admitted.discard(old)
                    self._admitted.add(session)
                try:
                    await asyncio.wait_for(old.close(), timeout=STALE_CLOSE_TIMEOUT)
                except asyncio.TimeoutError:
                    # Cleanup continues in the background (close() shields its
                    # work); the replacement retries binding if needed.
                    self._log.warning(
                        "stale session did not close within %.1fs; continuing", STALE_CLOSE_TIMEOUT
                    )
            self._instances[instance] = session

    async def _close_sessions(self) -> None:
        sessions = list(self._sessions)
        for session in sessions:
            await session.close()
        self._sessions.clear()
        self._admitted.clear()
        self._instances.clear()
        self._claim_locks.clear()

    async def stop(self) -> None:
        self._stop_event.set()
        await self._close_sessions()
