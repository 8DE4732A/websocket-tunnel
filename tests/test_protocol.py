import pytest

from websocket_tunnel.protocol import (
    InvalidFrameError,
    MessageType,
    decode,
    encode_control,
    encode_data,
)


def test_control_roundtrip():
    frame = encode_control(MessageType.STREAM_OPEN, {"stream_id": 7, "proxy": "web"})
    message_type, payload = decode(frame)
    assert message_type is MessageType.STREAM_OPEN
    assert payload == {"stream_id": 7, "proxy": "web"}


def test_data_roundtrip():
    chunk = b"x" * 100
    frame = encode_data(0xDEADBEEF, chunk)
    message_type, payload = decode(frame)
    assert message_type is MessageType.STREAM_DATA
    stream_id, data = payload
    assert stream_id == 0xDEADBEEF
    assert data == chunk


def test_ping():
    message_type, payload = decode(bytes([MessageType.PING]))
    assert message_type is MessageType.PING
    assert payload is None


def test_invalid_frames():
    with pytest.raises(InvalidFrameError):
        decode(b"")
    with pytest.raises(InvalidFrameError):
        decode(bytes([99]))
    with pytest.raises(InvalidFrameError):
        decode(bytes([MessageType.STREAM_DATA]) + b"\x00\x01")
    with pytest.raises(InvalidFrameError):
        decode(bytes([MessageType.HELLO]) + b"not json")
