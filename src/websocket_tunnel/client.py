"""Tunnel client: connects to a tunnel server and reconnects on failure."""

from __future__ import annotations

import asyncio
import logging
import ssl

import websockets
from websockets.asyncio.client import connect

from .config import ClientConfig, split_host_port
from .protocol import MAX_CONTROL_SIZE
from .session import Session

CONNECT_TIMEOUT = 15.0
RECONNECT_BASE = 1.0
RECONNECT_MAX = 30.0
RECONNECT_AFTER_SESSION = 1.0


def _ws_url(host: str, port: int, secure: bool) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = "wss" if secure else "ws"
    return f"{scheme}://{host}:{port}/"


class TunnelClient:
    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        self._log = logging.getLogger("wtunnel.client")
        self._session: Session | None = None
        self._stopping = False
        self._stop_wait = asyncio.Event()
        self.ready_event = asyncio.Event()

    async def run(self) -> None:
        backoff = RECONNECT_BASE
        while not self._stopping:
            try:
                ws = await asyncio.wait_for(self._connect(), CONNECT_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning(
                    "connect to %s failed: %s; retrying in %.1fs", self._config.server, exc, backoff
                )
                await self._sleep_until_stop_or(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
                continue
            backoff = RECONNECT_BASE
            self.ready_event = asyncio.Event()
            session = Session(
                ws,
                role="client",
                own_name="client",
                own_proxies=self._config.proxies,
                token=self._config.token,
                ready_event=self.ready_event,
                logger=self._log,
            )
            self._session = session
            try:
                await session.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("session failed: %s", exc)
            finally:
                self._session = None
                try:
                    await ws.close()
                except Exception:
                    pass
            if self._stopping:
                break
            self._log.info("session ended; reconnecting in %.1fs", RECONNECT_AFTER_SESSION)
            await self._sleep_until_stop_or(RECONNECT_AFTER_SESSION)

    async def _connect(self) -> websockets.asyncio.client.ClientConnection:
        ssl_context = self._build_ssl()
        host, port = split_host_port(self._config.server)
        uri = _ws_url(host, port, ssl_context is not None)
        return await connect(
            uri,
            ssl=ssl_context,
            max_size=MAX_CONTROL_SIZE,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=1,
            compression=None,
        )

    def _build_ssl(self) -> ssl.SSLContext | None:
        if not self._config.tls:
            return None
        context = ssl.create_default_context()
        if self._config.tls_skip_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def _sleep_until_stop_or(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_wait.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        self._stopping = True
        self._stop_wait.set()
        session = self._session
        if session is not None:
            await session.close()
