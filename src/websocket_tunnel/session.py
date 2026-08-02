"""Shared session logic for both tunnel nodes.

A session is one control WebSocket connection plus the proxies and
multiplexed TCP streams that live on it. The same code runs on the server
and the client: proxies may be declared on either side and the
``local``/``peer`` side fields decide where listeners bind and where
backends are dialed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from typing import Any

from . import __version__
from .config import ConfigError, ProxyConfig, AllowRule, is_endpoint_allowed, proxy_from_dict, split_host_port
from .protocol import (
    DATA_CHUNK_SIZE,
    PROTOCOL_VERSION,
    InvalidFrameError,
    MessageType,
    decode,
    encode_control,
    encode_data,
)

HANDSHAKE_TIMEOUT = 15.0
REGISTER_TIMEOUT = 10.0
OPEN_TIMEOUT = 10.0
STREAM_OPEN_TIMEOUT = 15.0
PING_INTERVAL = 30.0
_COUNTER_MAX = 0x7FFFFFFF
# Per-stream receive queue capacity (in 32 KiB frames). The queue is the
# elastic buffer between the multiplexed WebSocket and the local TCP socket;
# it must be large enough to absorb a full-duplex burst so that one direction
# of a relay can never deadlock the other.
_FLOW_QUEUE_SIZE = 4096
# Outbound priority-queue capacity.  Each entry is (priority, seq, frame).
# Control frames use _PRIO_CTRL (0), data frames use _PRIO_DATA (1), so
# STREAM_OPEN/CLOSE/ERROR are never head-of-line blocked by bulk data.
# The monotonic seq counter breaks ties within the same priority to preserve
# insertion order (avoids bytes comparison which would corrupt stream data).
_PRIO_CTRL = 0
_PRIO_DATA = 1
_SEND_QUEUE_SIZE = 4096 + 256


class Stream:
    """State for one multiplexed TCP stream (both directions)."""

    def __init__(
        self,
        stream_id: int,
        proxy_name: str,
        writer: asyncio.StreamWriter | None = None,
    ) -> None:
        self.stream_id = stream_id
        self.proxy_name = proxy_name
        self.writer = writer
        self.inbox: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_FLOW_QUEUE_SIZE)
        self.read_task: asyncio.Task[None] | None = None
        self.write_task: asyncio.Task[None] | None = None
        self.read_done = asyncio.Event()
        self.write_done = asyncio.Event()
        self.closed = asyncio.Event()
        self.open_event = asyncio.Event()
        self.open_ok = False
        self.open_reason = ""
        self.failed = False


def _flip_side(side: str) -> str:
    return "local" if side == "peer" else "peer"


def _proxy_payload(proxy: ProxyConfig) -> dict[str, Any]:
    return {
        "name": proxy.name,
        "listen": proxy.listen,
        "listen_side": proxy.listen_side,
        "backend": proxy.backend,
        "backend_side": proxy.backend_side,
    }


def _payload_stream_id(payload: Any) -> int | None:
    if isinstance(payload, dict) and isinstance(payload.get("stream_id"), int):
        return payload["stream_id"]
    return None


class Session:
    """One control connection between a tunnel server and a tunnel client."""

    def __init__(
        self,
        ws: Any,
        *,
        role: str,
        own_name: str,
        own_proxies: tuple[ProxyConfig, ...],
        token: str | None,
        allow_peer_backends: tuple[AllowRule, ...] = (),
        allow_peer_listens: tuple[AllowRule, ...] = (),
        ready_event: asyncio.Event,
        logger: logging.Logger,
    ) -> None:
        self._ws = ws
        self._role = role
        self._own_name = own_name
        self._own_proxies = own_proxies
        self._token = token
        self._allow_peer_backends = allow_peer_backends
        self._allow_peer_listens = allow_peer_listens
        self._ready = ready_event
        self._log = logger
        # Single priority queue: entries are (priority, seq, frame).
        # _PRIO_CTRL (0) sorts before _PRIO_DATA (1); seq preserves FIFO order
        # within the same priority level (prevents bytes comparison).
        self._send_queue: asyncio.PriorityQueue[tuple[int, int, bytes]] = asyncio.PriorityQueue(
            maxsize=_SEND_QUEUE_SIZE
        )
        self._send_seq = 0
        self._sender_task: asyncio.Task[None] | None = None
        self._proxies: dict[str, ProxyConfig] = {}
        self._listeners: dict[str, asyncio.AbstractServer] = {}
        self._streams: dict[int, Stream] = {}
        self._pending: dict[str, asyncio.Future[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._read_task: asyncio.Task[None] | None = None
        self._stream_next_id = 1
        self._closed = False
        self._peer_name = ""

    # ------------------------------------------------------------------ utils

    def _spawn(self, coro: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _ws_sender_loop(self) -> None:
        """Single task that owns the WebSocket write path.

        Dequeues frames from a PriorityQueue where control frames (_PRIO_CTRL=0)
        sort before data frames (_PRIO_DATA=1), so STREAM_OPEN/CLOSE/ERROR/PROXY_*
        are never head-of-line blocked by bulk TCP data.
        """
        try:
            while True:
                frame = (await self._send_queue.get())[2]
                await self._ws.send(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.debug("sender loop exited: %s", exc)

    async def _enqueue(self, priority: int, frame: bytes) -> None:
        self._send_seq += 1
        await self._send_queue.put((priority, self._send_seq, frame))

    async def _send_control(self, message_type: MessageType, payload: dict[str, Any]) -> None:
        await self._enqueue(_PRIO_CTRL, encode_control(message_type, payload))

    async def _send_stream_ctrl(self, message_type: MessageType, payload: dict[str, Any]) -> None:
        """Send a stream-scoped control frame (STREAM_CLOSE / STREAM_ERROR) at data priority.

        These frames must not leapfrog the data frames that preceded them on the
        same stream, so they share the _PRIO_DATA bucket rather than _PRIO_CTRL.
        """
        await self._enqueue(_PRIO_DATA, encode_control(message_type, payload))

    async def _send_data(self, stream_id: int, data: bytes) -> None:
        await self._enqueue(_PRIO_DATA, encode_data(stream_id, data))

    async def _safe_send_control(self, message_type: MessageType, payload: dict[str, Any]) -> None:
        try:
            await self._send_control(message_type, payload)
        except Exception as exc:
            self._log.debug("send %s failed: %s", message_type.name, exc)

    async def _send_error(self, stream_id: int, reason: str) -> None:
        await self._send_stream_ctrl(MessageType.STREAM_ERROR, {"stream_id": stream_id, "reason": reason})

    def _alloc_stream_id(self) -> int:
        # Stream ids must be unique across both nodes of a session, so each
        # node allocates from its own half of the 32-bit id space.
        role_bit = 1 if self._role == "server" else 0
        while True:
            sid = (role_bit << 31) | self._stream_next_id
            self._stream_next_id = 1 if self._stream_next_id == _COUNTER_MAX else self._stream_next_id + 1
            if sid not in self._streams:
                return sid

    def _maybe_finish(self, stream: Stream) -> None:
        if stream.read_done.is_set() and stream.write_done.is_set():
            stream.closed.set()

    def _remove_stream(self, stream_id: int) -> None:
        stream = self._streams.pop(stream_id, None)
        if stream is None:
            return
        stream.closed.set()
        if stream.read_task is not None:
            stream.read_task.cancel()
        if stream.write_task is not None:
            stream.write_task.cancel()
        if stream.writer is not None:
            try:
                stream.writer.close()
            except Exception:
                pass

    # ------------------------------------------------------------ handshake

    async def _direct_send(self, message_type: MessageType, payload: dict[str, Any]) -> None:
        """Send a control frame directly to the WebSocket, bypassing the send queues.

        Used only during the handshake phase before _ws_sender_loop is running.
        """
        await self._ws.send(encode_control(message_type, payload))

    async def _handshake(self) -> None:
        if self._role == "server":
            nonce = secrets.token_hex(32)
            await self._direct_send(
                MessageType.HELLO_CHALLENGE,
                {"nonce": nonce, "protocol": PROTOCOL_VERSION},
            )
            frame = await asyncio.wait_for(self._ws.recv(), HANDSHAKE_TIMEOUT)
            message_type, payload = decode(frame)
            if message_type is not MessageType.HELLO or not isinstance(payload, dict):
                raise ConfigError(f"expected HELLO, got {message_type.name}")
            if self._token is not None:
                response = payload.get("response")
                expected = hmac.new(
                    self._token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                if not isinstance(response, str) or not hmac.compare_digest(response, expected):
                    await self._direct_send(MessageType.HELLO_ERROR, {"reason": "authentication failed"})
                    raise ConfigError("authentication failed")
            self._peer_name = str(payload.get("name", ""))
            await self._direct_send(MessageType.HELLO_OK, {"node": "server", "version": __version__})
        else:
            frame = await asyncio.wait_for(self._ws.recv(), HANDSHAKE_TIMEOUT)
            message_type, payload = decode(frame)
            if message_type is not MessageType.HELLO_CHALLENGE or not isinstance(payload, dict):
                raise ConfigError(f"expected HELLO_CHALLENGE, got {message_type.name}")
            if payload.get("protocol") != PROTOCOL_VERSION:
                raise ConfigError(f"incompatible protocol version: {payload.get('protocol')!r}")
            nonce = str(payload.get("nonce", ""))
            response = hmac.new(
                (self._token or "").encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            await self._direct_send(
                MessageType.HELLO,
                {
                    "name": self._own_name,
                    "response": response,
                    "version": __version__,
                },
            )
            frame = await asyncio.wait_for(self._ws.recv(), HANDSHAKE_TIMEOUT)
            message_type, payload = decode(frame)
            if message_type is MessageType.HELLO_ERROR:
                reason = payload.get("reason") if isinstance(payload, dict) else ""
                raise ConfigError(f"handshake rejected: {reason}")
            if message_type is not MessageType.HELLO_OK:
                raise ConfigError(f"expected HELLO_OK, got {message_type.name}")

    # --------------------------------------------------------- registration

    async def _register_own_proxies(self) -> None:
        for proxy in self._own_proxies:
            await self._register_proxy(proxy)

    async def _register_proxy(self, proxy: ProxyConfig) -> None:
        if proxy.name in self._proxies:
            return
        self._proxies[proxy.name] = proxy
        try:
            if proxy.listen_side == "local":
                await self._bind_listener(proxy.name, proxy.listen)
            await self._send_control(MessageType.PROXY_REGISTER, _proxy_payload(proxy))
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._pending[proxy.name] = future
            await asyncio.wait_for(future, REGISTER_TIMEOUT)
        except Exception as exc:
            self._log.error("proxy '%s' registration failed: %s", proxy.name, exc)
            await self._drop_proxy(proxy.name)

    async def _bind_listener(self, name: str, addr: str) -> None:
        host, port = split_host_port(addr)

        def _accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self._spawn(self._handle_listener_conn(name, reader, writer))

        server = await asyncio.start_server(_accept, host, port, reuse_address=True)
        self._listeners[name] = server
        self._log.info("listener bound: %s -> %s", name, addr)

    async def _drop_proxy(self, name: str) -> None:
        self._proxies.pop(name, None)
        server = self._listeners.pop(name, None)
        if server is not None:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2)
            except Exception:
                pass
        self._pending.pop(name, None)

    def _check_peer_proxy_allowed(self, local: ProxyConfig) -> str | None:
        """Return an error reason string if the peer-requested proxy is not allowed, else None."""
        if local.listen_side == "local" and self._allow_peer_listens:
            listen_host, listen_port = split_host_port(local.listen)
            if not is_endpoint_allowed(listen_host, listen_port, self._allow_peer_listens):
                return f"listen address {local.listen!r} not in allow_peer_listens"
        if local.backend_side == "local" and self._allow_peer_backends:
            backend_host, backend_port = split_host_port(local.backend)
            if not is_endpoint_allowed(backend_host, backend_port, self._allow_peer_backends):
                return f"backend address {local.backend!r} not in allow_peer_backends"
        return None

    async def _handle_proxy_register(self, payload: dict[str, Any]) -> None:
        try:
            proxy = proxy_from_dict(payload)
        except ConfigError as exc:
            await self._safe_send_control(MessageType.PROXY_ERROR, {"name": payload.get("name"), "reason": str(exc)})
            return
        name = proxy.name
        if name in self._proxies:
            await self._safe_send_control(
                MessageType.PROXY_ERROR, {"name": name, "reason": f"proxy name conflict: {name}"}
            )
            return
        local = ProxyConfig(
            name=name,
            listen=proxy.listen,
            listen_side=_flip_side(proxy.listen_side),
            backend=proxy.backend,
            backend_side=_flip_side(proxy.backend_side),
        )
        denied = self._check_peer_proxy_allowed(local)
        if denied:
            self._log.warning("peer proxy '%s' rejected: %s", name, denied)
            await self._safe_send_control(MessageType.PROXY_ERROR, {"name": name, "reason": denied})
            return
        self._proxies[name] = local
        try:
            if local.listen_side == "local" and local.backend_side == "local":
                # The whole proxy lives on the peer; nothing for us to do.
                await self._safe_send_control(MessageType.PROXY_OK, {"name": name})
                return
            if local.listen_side == "local":
                await self._bind_listener(name, local.listen)
            await self._safe_send_control(MessageType.PROXY_OK, {"name": name})
        except Exception as exc:
            self._proxies.pop(name, None)
            await self._safe_send_control(MessageType.PROXY_ERROR, {"name": name, "reason": str(exc)})

    async def _handle_proxy_unregister(self, payload: dict[str, Any]) -> None:
        name = payload.get("name")
        if isinstance(name, str):
            await self._drop_proxy(name)
        await self._safe_send_control(MessageType.PROXY_OK, {"name": name if isinstance(name, str) else ""})

    async def _handle_proxy_result(self, message_type: MessageType, payload: dict[str, Any]) -> None:
        name = payload.get("name")
        if not isinstance(name, str):
            return
        future = self._pending.pop(name, None)
        if future is None or future.done():
            return
        if message_type is MessageType.PROXY_OK:
            future.set_result(None)
        else:
            future.set_exception(ConfigError(f"proxy '{name}' rejected by peer: {payload.get('reason', '')}"))

    # -------------------------------------------------------------- streams

    async def _handle_listener_conn(
        self,
        name: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        proxy = self._proxies.get(name)
        if proxy is None or self._closed:
            writer.close()
            return
        if proxy.listen_side == "local" and proxy.backend_side == "local":
            await self._relay_local(reader, writer, proxy.backend)
            return
        stream_id = self._alloc_stream_id()
        stream = Stream(stream_id, name, writer)
        self._streams[stream_id] = stream
        try:
            await self._send_control(MessageType.STREAM_OPEN, {"stream_id": stream_id, "proxy": name})
            await asyncio.wait_for(stream.open_event.wait(), STREAM_OPEN_TIMEOUT)
            if not stream.open_ok:
                raise ConnectionError(f"stream open rejected: {stream.open_reason or 'unknown error'}")
            await self._run_stream_halves(stream, reader)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning("stream %s via proxy '%s' failed: %s", stream_id, name, exc)
            await self._send_error(stream_id, str(exc))
        finally:
            self._remove_stream(stream_id)

    async def _handle_stream_open(self, stream: Stream) -> None:
        stream_id = stream.stream_id
        proxy = self._proxies.get(stream.proxy_name)
        if proxy is None:
            await self._send_error(stream_id, f"unknown proxy '{stream.proxy_name}'")
            self._remove_stream(stream_id)
            return
        if proxy.backend_side != "local":
            await self._send_error(stream_id, f"proxy '{stream.proxy_name}' is not served on this node")
            self._remove_stream(stream_id)
            return
        try:
            host, port = split_host_port(proxy.backend)
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), OPEN_TIMEOUT)
        except asyncio.CancelledError:
            self._remove_stream(stream_id)
            raise
        except Exception as exc:
            await self._send_error(stream_id, f"dial {proxy.backend} failed: {exc}")
            self._remove_stream(stream_id)
            return
        if stream.closed.is_set() or stream.write_done.is_set():
            # The peer gave up (or half-closed) while we were dialing.
            writer.close()
            self._remove_stream(stream_id)
            return
        stream.writer = writer
        await self._safe_send_control(MessageType.STREAM_OK, {"stream_id": stream_id})
        try:
            await self._run_stream_halves(stream, reader)
        finally:
            self._remove_stream(stream_id)

    async def _run_stream_halves(self, stream: Stream, reader: asyncio.StreamReader) -> None:
        stream.read_task = self._spawn(self._tcp_to_ws(stream, reader))
        stream.write_task = self._spawn(self._ws_to_tcp(stream))
        await stream.closed.wait()

    async def _tcp_to_ws(self, stream: Stream, reader: asyncio.StreamReader) -> None:
        try:
            while not stream.closed.is_set():
                data = await reader.read(DATA_CHUNK_SIZE)
                if not data:
                    break
                await self._send_data(stream.stream_id, data)
            await self._send_stream_ctrl(MessageType.STREAM_CLOSE, {"stream_id": stream.stream_id})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.debug("stream %s read side failed: %s", stream.stream_id, exc)
            stream.failed = True
            await self._send_error(stream.stream_id, str(exc))
            stream.closed.set()
        finally:
            stream.read_done.set()
            self._maybe_finish(stream)

    async def _ws_to_tcp(self, stream: Stream) -> None:
        """Drain the stream's inbox into the local TCP socket."""
        try:
            while not stream.closed.is_set():
                chunk = await stream.inbox.get()
                if chunk is None:
                    break
                if stream.writer is None:
                    continue
                stream.writer.write(chunk)
                await stream.writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.debug("stream %s write side failed: %s", stream.stream_id, exc)
            stream.failed = True
            await self._send_error(stream.stream_id, str(exc))
            stream.closed.set()
        finally:
            if stream.writer is not None:
                try:
                    stream.writer.write_eof()
                except Exception:
                    pass
            stream.write_done.set()
            self._maybe_finish(stream)

    async def _relay_local(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        backend: str,
    ) -> None:
        """Relay between two local endpoints without using the tunnel."""
        host, port = split_host_port(backend)
        try:
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), OPEN_TIMEOUT
            )
        except Exception as exc:
            self._log.warning("local relay dial %s failed: %s", backend, exc)
            client_writer.close()
            return

        queues = (
            asyncio.Queue[bytes | None](maxsize=_FLOW_QUEUE_SIZE),
            asyncio.Queue[bytes | None](maxsize=_FLOW_QUEUE_SIZE),
        )

        async def producer(src: asyncio.StreamReader, queue: asyncio.Queue[bytes | None], name: str) -> None:
            try:
                while True:
                    data = await src.read(DATA_CHUNK_SIZE)
                    if not data:
                        break
                    await queue.put(data)
                await queue.put(None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.debug("local relay %s read failed: %s", name, exc)

        async def consumer(queue: asyncio.Queue[bytes | None], dst: asyncio.StreamWriter, name: str) -> None:
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.debug("local relay %s write failed: %s", name, exc)
            finally:
                try:
                    dst.write_eof()
                except Exception:
                    pass

        tasks = [
            asyncio.create_task(producer(client_reader, queues[0], "client->backend")),
            asyncio.create_task(consumer(queues[0], backend_writer, "client->backend")),
            asyncio.create_task(producer(backend_reader, queues[1], "backend->client")),
            asyncio.create_task(consumer(queues[1], client_writer, "backend->client")),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled():
                    task.exception()  # retrieve to avoid "never retrieved" warnings
        except asyncio.CancelledError:
            raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for writer in (client_writer, backend_writer):
                try:
                    writer.close()
                except Exception:
                    pass

    # --------------------------------------------------------------- frames

    async def _handle_data(self, stream_id: int, chunk: bytes) -> None:
        stream = self._streams.get(stream_id)
        if stream is None:
            return
        try:
            stream.inbox.put_nowait(chunk)
        except asyncio.QueueFull:
            self._log.warning("stream %s receive buffer exceeded", stream_id)
            stream.failed = True
            await self._send_error(stream_id, "receive buffer exceeded")
            stream.closed.set()

    async def _handle_stream_close(self, payload: Any) -> None:
        stream_id = _payload_stream_id(payload)
        if stream_id is None:
            return
        stream = self._streams.get(stream_id)
        if stream is None:
            return
        if stream.write_done.is_set():
            return
        try:
            stream.inbox.put_nowait(None)  # flush queued data, then EOF
        except asyncio.QueueFull:
            stream.failed = True
            await self._send_error(stream_id, "receive buffer exceeded")
            stream.closed.set()

    async def _handle_stream_result(self, message_type: MessageType, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if "stream_id" in payload:
            stream_id = payload.get("stream_id")
            if not isinstance(stream_id, int):
                return
            stream = self._streams.get(stream_id)
            if stream is None:
                return
            if message_type is MessageType.STREAM_OK:
                stream.open_ok = True
                stream.open_event.set()
            else:
                if not stream.open_event.is_set():
                    stream.open_ok = False
                    stream.open_reason = str(payload.get("reason", ""))
                    stream.open_event.set()
                stream.failed = True
                stream.closed.set()
            return
        if "name" in payload:
            await self._handle_proxy_result(message_type, payload)

    async def _dispatch(self, message_type: MessageType, payload: Any) -> None:
        if message_type is MessageType.PING:
            await self._safe_send_control(MessageType.PONG, {})
            return
        if message_type in (MessageType.PONG, MessageType.HELLO, MessageType.HELLO_OK, MessageType.HELLO_ERROR, MessageType.HELLO_CHALLENGE):
            return
        if message_type is MessageType.STREAM_DATA:
            stream_id, chunk = payload
            await self._handle_data(stream_id, chunk)
            return
        if message_type is MessageType.STREAM_OPEN:
            if isinstance(payload, dict):
                stream_id = payload.get("stream_id")
                name = payload.get("proxy")
                if isinstance(stream_id, int) and isinstance(name, str) and stream_id not in self._streams:
                    stream = Stream(stream_id, name)
                    self._streams[stream_id] = stream
                    self._spawn(self._handle_stream_open(stream))
                elif isinstance(stream_id, int):
                    self._spawn(self._send_error(stream_id, "duplicate stream id"))
            return
        if message_type in (MessageType.STREAM_OK, MessageType.STREAM_ERROR):
            await self._handle_stream_result(message_type, payload)
            return
        if message_type is MessageType.STREAM_CLOSE:
            await self._handle_stream_close(payload)
            return
        if message_type is MessageType.PROXY_REGISTER:
            if isinstance(payload, dict):
                await self._handle_proxy_register(payload)
            return
        if message_type is MessageType.PROXY_UNREGISTER:
            if isinstance(payload, dict):
                await self._handle_proxy_unregister(payload)
            return
        if message_type in (MessageType.PROXY_OK, MessageType.PROXY_ERROR):
            if isinstance(payload, dict):
                await self._handle_proxy_result(message_type, payload)

    async def _read_loop(self) -> None:
        try:
            while True:
                frame = await self._ws.recv()
                try:
                    message_type, payload = decode(frame)
                except InvalidFrameError as exc:
                    self._log.warning("invalid frame dropped: %s", exc)
                    continue
                await self._dispatch(message_type, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.info("connection closed: %s", exc)

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                await self._safe_send_control(MessageType.PING, {})
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        try:
            await self._handshake()
            self._log.info("handshake complete (peer=%r)", self._peer_name)
        except Exception as exc:
            self._log.error("handshake failed: %s", exc)
            await self.close()
            return
        self._sender_task = self._spawn(self._ws_sender_loop())
        self._spawn(self._ping_loop())
        self._read_task = self._spawn(self._read_loop())
        try:
            await self._register_own_proxies()
            if not self._read_task.done() and not self._closed:
                self._ready.set()
                self._log.info("session ready with %d proxy(ies)", len(self._proxies))
            await self._read_task
        except asyncio.CancelledError:
            raise
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for stream_id in list(self._streams):
            self._remove_stream(stream_id)
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("session closed"))
        self._pending.clear()
        for task in list(self._tasks):
            if task is self._read_task:
                continue
            task.cancel()
        if self._tasks:
            await asyncio.gather(*(t for t in self._tasks if t is not self._read_task), return_exceptions=True)
        if self._read_task is not None:
            self._tasks.discard(self._read_task)
        for server in list(self._listeners.values()):
            server.close()
        for server in list(self._listeners.values()):
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2)
            except Exception:
                pass
        self._listeners.clear()
        try:
            await self._ws.close()
        except Exception:
            pass
