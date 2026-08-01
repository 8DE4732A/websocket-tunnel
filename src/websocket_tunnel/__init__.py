"""websocket-tunnel: a frp-like intranet penetration tool over WebSocket."""

from __future__ import annotations

try:
    from importlib.metadata import version
    __version__: str = version("websocket-tunnel")
except Exception:
    __version__ = "0.0.0"
