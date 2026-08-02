from __future__ import annotations

import asyncio
import hashlib
import http.server
import os
import socket
import threading

import pytest

from websocket_tunnel.client import TunnelClient
from websocket_tunnel.config import ClientConfig, ProxyConfig, ServerConfig
from websocket_tunnel.server import TunnelServer

BACKEND = "127.0.0.1"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((BACKEND, 0))
        return sock.getsockname()[1]


async def start_echo() -> tuple[asyncio.Server, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, BACKEND, 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def wait_ready(obj: object, timeout: float = 8.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        event = obj.ready_event
        if event.is_set():
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"{type(obj).__name__} not ready within {timeout}s")
        try:
            await asyncio.wait_for(event.wait(), min(1.0, remaining))
        except asyncio.TimeoutError:
            continue


async def start_pair(
    server_cfg: ServerConfig,
    client_cfg: ClientConfig,
    *,
    wait_server: bool = False,
):
    server = TunnelServer(server_cfg)
    client = TunnelClient(client_cfg)
    server_task = asyncio.create_task(server.run())
    client_task = asyncio.create_task(client.run())
    try:
        await wait_ready(client)
        if wait_server:
            await wait_ready(server)
    except Exception:
        await cleanup(server, client, server_task, client_task)
        raise
    return server, client, server_task, client_task


async def cleanup(server: TunnelServer, client: TunnelClient, server_task, client_task) -> None:
    await client.stop()
    await server.stop()
    await asyncio.gather(client_task, server_task, return_exceptions=True)


async def echo_once(port: int, payload: bytes, timeout: float = 10.0) -> bytes:
    reader, writer = await asyncio.open_connection(BACKEND, port)
    try:
        writer.write(payload)
        await writer.drain()
        received = b""
        while len(received) < len(payload):
            data = await asyncio.wait_for(reader.read(65536), timeout)
            if not data:
                break
            received += data
        return received
    finally:
        writer.close()
        await writer.wait_closed()


def classic_client_cfg(server_port: int, listen_port: int, backend_port: int, **kwargs) -> ClientConfig:
    return ClientConfig(
        server=f"{BACKEND}:{server_port}",
        token="t",
        proxies=(ProxyConfig("echo", f"{BACKEND}:{listen_port}", "peer", f"{BACKEND}:{backend_port}", "local"),),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_classic_proxy():
    backend, backend_port = await start_echo()
    server_port = free_port()
    listen_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="t")
    client_cfg = classic_client_cfg(server_port, listen_port, backend_port)
    server, client, server_task, client_task = await start_pair(server_cfg, client_cfg)
    try:
        payload = b"hello through the tunnel" * 100
        assert await echo_once(listen_port, payload) == payload
    finally:
        await cleanup(server, client, server_task, client_task)
        backend.close()
        await backend.wait_closed()


@pytest.mark.asyncio
async def test_reverse_proxy_declared_by_client():
    backend, backend_port = await start_echo()
    server_port = free_port()
    listen_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="t")
    client_cfg = ClientConfig(
        server=f"{BACKEND}:{server_port}",
        token="t",
        proxies=(ProxyConfig("rev", f"{BACKEND}:{listen_port}", "local", f"{BACKEND}:{backend_port}", "peer"),),
    )
    server, client, server_task, client_task = await start_pair(server_cfg, client_cfg)
    try:
        payload = os.urandom(4096)
        assert await echo_once(listen_port, payload) == payload
    finally:
        await cleanup(server, client, server_task, client_task)
        backend.close()
        await backend.wait_closed()


@pytest.mark.asyncio
async def test_reverse_proxy_declared_by_server():
    backend, backend_port = await start_echo()
    server_port = free_port()
    listen_port = free_port()
    server_cfg = ServerConfig(
        listen=f"{BACKEND}:{server_port}",
        token="t",
        proxies=(ProxyConfig("rev", f"{BACKEND}:{listen_port}", "peer", f"{BACKEND}:{backend_port}", "local"),),
    )
    client_cfg = ClientConfig(server=f"{BACKEND}:{server_port}", token="t")
    server, client, server_task, client_task = await start_pair(server_cfg, client_cfg, wait_server=True)
    try:
        payload = os.urandom(4096)
        assert await echo_once(listen_port, payload) == payload
    finally:
        await cleanup(server, client, server_task, client_task)
        backend.close()
        await backend.wait_closed()


@pytest.mark.asyncio
async def test_local_only_proxy_on_server():
    backend, backend_port = await start_echo()
    server_port = free_port()
    listen_port = free_port()
    server_cfg = ServerConfig(
        listen=f"{BACKEND}:{server_port}",
        token="t",
        proxies=(ProxyConfig("local", f"{BACKEND}:{listen_port}", "local", f"{BACKEND}:{backend_port}", "local"),),
    )
    client_cfg = ClientConfig(server=f"{BACKEND}:{server_port}", token="t")
    server, client, server_task, client_task = await start_pair(server_cfg, client_cfg, wait_server=True)
    try:
        payload = b"local-only relay"
        assert await echo_once(listen_port, payload) == payload
    finally:
        await cleanup(server, client, server_task, client_task)
        backend.close()
        await backend.wait_closed()


@pytest.mark.asyncio
async def test_concurrent_streams():
    backend, backend_port = await start_echo()
    server_port = free_port()
    listen_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="t")
    client_cfg = classic_client_cfg(server_port, listen_port, backend_port)
    server, client, server_task, client_task = await start_pair(server_cfg, client_cfg)
    try:
        async def one(i: int) -> None:
            payload = f"stream-{i}-".encode() * 2000
            assert await echo_once(listen_port, payload) == payload

        await asyncio.gather(*(one(i) for i in range(50)))
    finally:
        await cleanup(server, client, server_task, client_task)
        backend.close()
        await backend.wait_closed()


@pytest.mark.asyncio
async def test_large_transfer():
    backend, backend_port = await start_echo()
    server_port = free_port()
    listen_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="t")
    client_cfg = classic_client_cfg(server_port, listen_port, backend_port)
    server, client, server_task, client_task = await start_pair(server_cfg, client_cfg)
    try:
        payload = os.urandom(32 * 1024 * 1024)
        received = await echo_once(listen_port, payload, timeout=60)
        assert hashlib.sha256(received).digest() == hashlib.sha256(payload).digest()
    finally:
        await cleanup(server, client, server_task, client_task)
        backend.close()
        await backend.wait_closed()


class _HttpHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = b"hello-" + self.path.encode() + b"-" * 128
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def start_http_backend() -> tuple[http.server.ThreadingHTTPServer, threading.Thread, int]:
    httpd = http.server.ThreadingHTTPServer((BACKEND, 0), _HttpHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, port


def stop_http_backend(httpd: http.server.ThreadingHTTPServer, thread: threading.Thread) -> None:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


async def read_http_response(reader: asyncio.StreamReader, timeout: float = 5.0) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await asyncio.wait_for(reader.read(4096), timeout)
        if not chunk:
            break
        data += chunk
    head, separator, rest = data.partition(b"\r\n\r\n")
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        key, _, value = line.partition(b":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get(b"content-length", b"0"))
    while len(rest) < length:
        chunk = await asyncio.wait_for(reader.read(4096), timeout)
        if not chunk:
            break
        rest += chunk
    return head + separator + rest


@pytest.mark.asyncio
async def test_http_keep_alive():
    httpd, thread, backend_port = start_http_backend()
    server_port = free_port()
    listen_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="t")
    client_cfg = classic_client_cfg(server_port, listen_port, backend_port)
    server, client, server_task, client_task = await start_pair(server_cfg, client_cfg)
    try:
        reader, writer = await asyncio.open_connection(BACKEND, listen_port)
        try:
            writer.write(b"GET /one HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n")
            await writer.drain()
            response1 = await read_http_response(reader)
            assert response1.startswith(b"HTTP/1.1 200")
            assert b"hello-/one" in response1

            writer.write(b"GET /two HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n")
            await writer.drain()
            response2 = await read_http_response(reader)
            assert response2.startswith(b"HTTP/1.1 200")
            assert b"hello-/two" in response2
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await cleanup(server, client, server_task, client_task)
        stop_http_backend(httpd, thread)


@pytest.mark.asyncio
async def test_http10_half_close():
    httpd, thread, backend_port = start_http_backend()
    server_port = free_port()
    listen_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="t")
    client_cfg = classic_client_cfg(server_port, listen_port, backend_port)
    server, client, server_task, client_task = await start_pair(server_cfg, client_cfg)
    try:
        reader, writer = await asyncio.open_connection(BACKEND, listen_port)
        try:
            writer.write(b"GET /hc HTTP/1.0\r\n\r\n")
            await writer.drain()
            writer.write_eof()  # half-close: request side done, response may still flow
            data = b""
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), 5)
                if not chunk:
                    break
                data += chunk
            assert b" 200 " in data.split(b"\r\n", 1)[0]
            assert b"hello-/hc" in data
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await cleanup(server, client, server_task, client_task)
        stop_http_backend(httpd, thread)


@pytest.mark.asyncio
async def test_auth_rejected():
    server_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="correct")
    client_cfg = ClientConfig(server=f"{BACKEND}:{server_port}", token="wrong")
    server = TunnelServer(server_cfg)
    client = TunnelClient(client_cfg)
    server_task = asyncio.create_task(server.run())
    client_task = asyncio.create_task(client.run())
    with pytest.raises(TimeoutError):
        await wait_ready(client, timeout=2.5)
    await cleanup(server, client, server_task, client_task)


@pytest.mark.asyncio
async def test_tls_with_self_signed(tmp_path):
    trustme = pytest.importorskip("trustme")
    ca = trustme.CA()
    server_cert = ca.issue_cert("localhost")
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    server_cert.cert_chain_pems[0].write_to_path(cert_path)
    server_cert.private_key_pem.write_to_path(key_path)

    backend, backend_port = await start_echo()
    server_port = free_port()
    listen_port = free_port()
    server_cfg = ServerConfig(
        listen=f"{BACKEND}:{server_port}",
        token="t",
        tls_cert=str(cert_path),
        tls_key=str(key_path),
    )
    client_cfg = classic_client_cfg(server_port, listen_port, backend_port, tls=True, tls_skip_verify=True)
    server, client, server_task, client_task = await start_pair(server_cfg, client_cfg)
    try:
        payload = b"secure payload"
        assert await echo_once(listen_port, payload) == payload
    finally:
        await cleanup(server, client, server_task, client_task)
        backend.close()
        await backend.wait_closed()


@pytest.mark.asyncio
async def test_tls_verification_enforced(tmp_path):
    trustme = pytest.importorskip("trustme")
    ca = trustme.CA()
    server_cert = ca.issue_cert("localhost")
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    server_cert.cert_chain_pems[0].write_to_path(cert_path)
    server_cert.private_key_pem.write_to_path(key_path)

    server_port = free_port()
    server_cfg = ServerConfig(
        listen=f"{BACKEND}:{server_port}",
        token="t",
        tls_cert=str(cert_path),
        tls_key=str(key_path),
    )
    client_cfg = ClientConfig(server=f"{BACKEND}:{server_port}", token="t", tls=True, tls_skip_verify=False)
    server = TunnelServer(server_cfg)
    client = TunnelClient(client_cfg)
    server_task = asyncio.create_task(server.run())
    client_task = asyncio.create_task(client.run())
    with pytest.raises(TimeoutError):
        await wait_ready(client, timeout=2.5)
    await cleanup(server, client, server_task, client_task)


@pytest.mark.asyncio
async def test_reconnect_after_server_drop():
    backend, backend_port = await start_echo()
    server_port = free_port()
    listen_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="t")
    client_cfg = classic_client_cfg(server_port, listen_port, backend_port)
    server, client, server_task, client_task = await start_pair(server_cfg, client_cfg)
    try:
        assert await echo_once(listen_port, b"before-drop") == b"before-drop"
        old_ready = client.ready_event
        await server._close_sessions()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while loop.time() < deadline:
            event = client.ready_event
            if event is not old_ready and event.is_set():
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("client did not reconnect")

        assert await echo_once(listen_port, b"after-reconnect") == b"after-reconnect"
    finally:
        await cleanup(server, client, server_task, client_task)
        backend.close()
        await backend.wait_closed()


@pytest.mark.asyncio
async def test_max_connections_rejects_excess():
    """A second client is rejected when max_connections=1."""
    server_port = free_port()
    listen1 = free_port()
    listen2 = free_port()
    backend, backend_port = await start_echo()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="t", max_connections=1)

    def make_cfg(port: int) -> ClientConfig:
        return ClientConfig(
            server=f"{BACKEND}:{server_port}",
            token="t",
            proxies=(ProxyConfig(f"p{port}", f"{BACKEND}:{port}", "peer", f"{BACKEND}:{backend_port}", "local"),),
        )

    server = TunnelServer(server_cfg)
    client1 = TunnelClient(make_cfg(listen1))
    server_task = asyncio.create_task(server.run())
    client1_task = asyncio.create_task(client1.run())
    try:
        # client1 must be fully ready before client2 is started so it wins the slot.
        await wait_ready(client1)
        assert await echo_once(listen1, b"ok") == b"ok"

        client2 = TunnelClient(make_cfg(listen2))
        client2_task = asyncio.create_task(client2.run())
        try:
            # client2 should never become ready; the only slot is taken.
            with pytest.raises(TimeoutError):
                await wait_ready(client2, timeout=2.5)
        finally:
            await client2.stop()
            await asyncio.gather(client2_task, return_exceptions=True)
    finally:
        await client1.stop()
        await server.stop()
        await asyncio.gather(client1_task, server_task, return_exceptions=True)
        backend.close()
        await backend.wait_closed()


@pytest.mark.asyncio
async def test_allow_peer_listens_blocks_denied_proxy():
    """Server rejects a client proxy that asks the server to bind outside allow_peer_listens."""
    server_port = free_port()
    listen_port = free_port()
    # Server only allows binding on 10.0.0.0/8; client asks to bind on 127.0.0.1.
    server_cfg = ServerConfig(
        listen=f"{BACKEND}:{server_port}",
        token="t",
        allow_peer_listens=("10.0.0.0/8",),
    )
    # Classic proxy: listen on server (peer from client's view), backend on client (local).
    client_cfg = ClientConfig(
        server=f"{BACKEND}:{server_port}",
        token="t",
        proxies=(ProxyConfig("blocked", f"{BACKEND}:{listen_port}", "peer", f"{BACKEND}:9999", "local"),),
    )
    server = TunnelServer(server_cfg)
    client = TunnelClient(client_cfg)
    server_task = asyncio.create_task(server.run())
    client_task = asyncio.create_task(client.run())
    try:
        # Session is established but server rejects the proxy; listener is never bound.
        await wait_ready(client)
        with pytest.raises(OSError):
            await asyncio.wait_for(asyncio.open_connection(BACKEND, listen_port), timeout=1.0)
    finally:
        await client.stop()
        await server.stop()
        await asyncio.gather(client_task, server_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_allow_peer_backends_blocks_denied_proxy():
    """Server rejects a client proxy whose backend (dialed by server) is outside allow_peer_backends."""
    server_port = free_port()
    listen_port = free_port()
    # Reverse proxy: listen on client (local from client's view), backend on server (peer from client).
    # After flip on server: listen_side=peer (client binds), backend_side=local (server dials).
    # Server only allows dialing 127.0.0.1/32; client asks server to dial 10.0.0.1.
    server_cfg = ServerConfig(
        listen=f"{BACKEND}:{server_port}",
        token="t",
        allow_peer_backends=("127.0.0.1/32",),
    )
    client_cfg = ClientConfig(
        server=f"{BACKEND}:{server_port}",
        token="t",
        proxies=(ProxyConfig("blocked", f"{BACKEND}:{listen_port}", "local", "10.0.0.1:9999", "peer"),),
    )
    server = TunnelServer(server_cfg)
    client = TunnelClient(client_cfg)
    server_task = asyncio.create_task(server.run())
    client_task = asyncio.create_task(client.run())
    try:
        # Session is established but server rejects the proxy; listener on client is never bound.
        await wait_ready(client)
        with pytest.raises(OSError):
            await asyncio.wait_for(asyncio.open_connection(BACKEND, listen_port), timeout=1.0)
    finally:
        await client.stop()
        await server.stop()
        await asyncio.gather(client_task, server_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_two_clients():
    backend, backend_port = await start_echo()
    server_port = free_port()
    listen1 = free_port()
    listen2 = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="t")

    def client_cfg(port: int) -> ClientConfig:
        return ClientConfig(
            server=f"{BACKEND}:{server_port}",
            token="t",
            proxies=(ProxyConfig(f"p{port}", f"{BACKEND}:{port}", "peer", f"{BACKEND}:{backend_port}", "local"),),
        )

    server = TunnelServer(server_cfg)
    client1 = TunnelClient(client_cfg(listen1))
    client2 = TunnelClient(client_cfg(listen2))
    server_task = asyncio.create_task(server.run())
    client1_task = asyncio.create_task(client1.run())
    client2_task = asyncio.create_task(client2.run())
    try:
        await wait_ready(client1)
        await wait_ready(client2)
        assert await echo_once(listen1, b"c1") == b"c1"
        assert await echo_once(listen2, b"c2") == b"c2"
    finally:
        await client1.stop()
        await client2.stop()
        await server.stop()
        await asyncio.gather(client1_task, client2_task, server_task, return_exceptions=True)
        backend.close()
        await backend.wait_closed()


@pytest.mark.asyncio
async def test_challenge_response_correct_token():
    """客户端用正确 token 能完成 challenge-response 握手并建立会话。"""
    server_port = free_port()
    backend, backend_port = await start_echo()
    listen_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="s3cr3t")
    client_cfg = ClientConfig(
        server=f"{BACKEND}:{server_port}",
        token="s3cr3t",
        proxies=(ProxyConfig("echo", f"{BACKEND}:{listen_port}", "peer", f"{BACKEND}:{backend_port}", "local"),),
    )
    server = TunnelServer(server_cfg)
    client = TunnelClient(client_cfg)
    server_task = asyncio.create_task(server.run())
    client_task = asyncio.create_task(client.run())
    try:
        await wait_ready(client)
        data = await echo_once(listen_port, b"hello-cr")
        assert data == b"hello-cr"
    finally:
        await cleanup(server, client, server_task, client_task)
        backend.close()
        await backend.wait_closed()


@pytest.mark.asyncio
async def test_challenge_response_wrong_token_rejected():
    """客户端用错误 token 计算的 HMAC 响应应被服务端拒绝。"""
    server_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}", token="correct")
    client_cfg = ClientConfig(server=f"{BACKEND}:{server_port}", token="wrong")
    server = TunnelServer(server_cfg)
    client = TunnelClient(client_cfg)
    server_task = asyncio.create_task(server.run())
    client_task = asyncio.create_task(client.run())
    with pytest.raises(TimeoutError):
        await wait_ready(client, timeout=2.5)
    await cleanup(server, client, server_task, client_task)


@pytest.mark.asyncio
async def test_challenge_response_no_token_server():
    """服务端不配置 token 时，任何客户端均可连接（向后兼容无认证模式）。"""
    server_port = free_port()
    backend, backend_port = await start_echo()
    listen_port = free_port()
    server_cfg = ServerConfig(listen=f"{BACKEND}:{server_port}")
    client_cfg = ClientConfig(
        server=f"{BACKEND}:{server_port}",
        proxies=(ProxyConfig("echo", f"{BACKEND}:{listen_port}", "peer", f"{BACKEND}:{backend_port}", "local"),),
    )
    server = TunnelServer(server_cfg)
    client = TunnelClient(client_cfg)
    server_task = asyncio.create_task(server.run())
    client_task = asyncio.create_task(client.run())
    try:
        await wait_ready(client)
        data = await echo_once(listen_port, b"no-auth")
        assert data == b"no-auth"
    finally:
        await cleanup(server, client, server_task, client_task)
        backend.close()
        await backend.wait_closed()
