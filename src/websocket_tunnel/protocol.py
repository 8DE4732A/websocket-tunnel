"""Binary wire protocol shared by tunnel nodes.

Every WebSocket message is one binary frame:

* control messages: 1 type byte + UTF-8 JSON object
* stream data:      1 type byte + 4-byte big-endian stream id + raw chunk
* ping/pong:        1 type byte
"""

from __future__ import annotations

import json
from enum import IntEnum
from typing import Any


class MessageType(IntEnum):
    HELLO = 1
    HELLO_OK = 2
    HELLO_ERROR = 3
    PROXY_REGISTER = 4
    PROXY_OK = 5
    PROXY_ERROR = 6
    PROXY_UNREGISTER = 7
    STREAM_OPEN = 8
    STREAM_OK = 9
    STREAM_ERROR = 10
    STREAM_DATA = 11
    STREAM_CLOSE = 12
    PING = 13
    PONG = 14


PROTOCOL_VERSION = 1
DATA_CHUNK_SIZE = 32 * 1024
MAX_CONTROL_SIZE = 1 << 20
STREAM_ID_SIZE = 4


class InvalidFrameError(ValueError):
    """Raised when a frame cannot be decoded."""


def encode_control(message_type: MessageType, payload: dict[str, Any]) -> bytes:
    return bytes([message_type]) + json.dumps(payload, separators=(",", ":")).encode("utf-8")


def encode_data(stream_id: int, chunk: bytes) -> bytes:
    return bytes([MessageType.STREAM_DATA]) + stream_id.to_bytes(STREAM_ID_SIZE, "big") + chunk


def encode_ping() -> bytes:
    return bytes([MessageType.PING])


def encode_pong() -> bytes:
    return bytes([MessageType.PONG])


def decode(frame: bytes) -> tuple[MessageType, Any]:
    if not frame:
        raise InvalidFrameError("empty frame")
    try:
        message_type = MessageType(frame[0])
    except ValueError as exc:
        raise InvalidFrameError(f"unknown message type {frame[0]}") from exc
    payload = frame[1:]
    if message_type is MessageType.STREAM_DATA:
        if len(payload) < STREAM_ID_SIZE:
            raise InvalidFrameError("stream data frame too short")
        stream_id = int.from_bytes(payload[:STREAM_ID_SIZE], "big")
        return message_type, (stream_id, payload[STREAM_ID_SIZE:])
    if message_type in (MessageType.PING, MessageType.PONG):
        return message_type, None
    try:
        return message_type, json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidFrameError(f"invalid JSON control payload: {exc}") from exc
