import asyncio
import logging

from websocket_tunnel.session import Session, _COUNTER_MAX


class DummyWS:
    async def send(self, data):
        pass

    async def recv(self):
        raise asyncio.CancelledError

    async def close(self):
        pass


def make_session(role="client"):
    return Session(
        DummyWS(),
        role=role,
        own_name="test",
        own_proxies=(),
        token=None,
        ready_event=asyncio.Event(),
        logger=logging.getLogger("test-session"),
    )


def test_stream_id_wrap_skips_occupied():
    session = make_session(role="client")
    session._stream_next_id = _COUNTER_MAX - 1
    session._streams[_COUNTER_MAX] = object()  # occupied id
    assert session._alloc_stream_id() == _COUNTER_MAX - 1
    assert session._alloc_stream_id() == 1  # wrapped, skipped occupied id
    assert session._alloc_stream_id() == 2


def test_stream_id_namespaces_by_role():
    client = make_session(role="client")
    server = make_session(role="server")
    ids = {client._alloc_stream_id() for _ in range(10)}
    ids.update(server._alloc_stream_id() for _ in range(10))
    assert len(ids) == 20  # no collisions between roles
    assert all(0 <= sid <= 0xFFFFFFFF for sid in ids)
