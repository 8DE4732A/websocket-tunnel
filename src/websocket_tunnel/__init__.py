"""websocket-tunnel: a frp-like intranet penetration tool over WebSocket."""

from __future__ import annotations

try:
    from importlib.metadata import version, PackageNotFoundError
    try:
        __version__ = version("websocket-tunnel")
    except PackageNotFoundError:
        from ._version import __version__  # type: ignore[no-reattr]
except Exception:
    __version__ = "0.0.0"
