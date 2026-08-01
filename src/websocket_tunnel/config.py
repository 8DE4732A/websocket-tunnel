"""TOML configuration parsing and validation for both tunnel nodes."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HOST_PORT_RE = re.compile(r"^(\[[^\[\]]+\]|[^:]+):([0-9]+)$")


class ConfigError(Exception):
    """Raised when a configuration file or value is invalid."""


@dataclass(frozen=True)
class ProxyConfig:
    name: str
    listen: str
    listen_side: str  # "local" | "peer"
    backend: str
    backend_side: str  # "local" | "peer"


@dataclass(frozen=True)
class ServerConfig:
    listen: str = "0.0.0.0:7000"
    token: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    proxies: tuple[ProxyConfig, ...] = ()
    max_connections: int = 0
    # CIDR networks/addresses that peer-registered backends may target.
    # Empty list = allow all (default for backward compatibility).
    allow_peer_backends: tuple[str, ...] = ()
    # CIDR networks/addresses that peers may ask this node to bind listeners on.
    allow_peer_listens: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClientConfig:
    server: str = "127.0.0.1:7000"
    token: str | None = None
    tls: bool = False
    tls_skip_verify: bool = False
    proxies: tuple[ProxyConfig, ...] = ()
    max_connections: int = 0
    allow_peer_backends: tuple[str, ...] = ()
    allow_peer_listens: tuple[str, ...] = ()


def _parse_cidr_list(raw: Any, key: str) -> tuple[str, ...]:
    """Validate a list of CIDR/IP strings from config."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"'{key}' must be a list of CIDR strings")
    result = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError(f"'{key}' entries must be strings, got {item!r}")
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ConfigError(f"invalid CIDR in '{key}': {item!r} — {exc}") from exc
        result.append(item)
    return tuple(result)


def is_host_allowed(host: str, allowed_cidrs: tuple[str, ...]) -> bool:
    """Return True when *host* falls within any of the allowed CIDR networks.

    An empty *allowed_cidrs* list means "allow all".
    """
    if not allowed_cidrs:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Non-IP hostnames (e.g. "localhost") are never allowed when a whitelist
        # is active — callers should resolve or use IP addresses in the config.
        return False
    return any(addr in ipaddress.ip_network(cidr, strict=False) for cidr in allowed_cidrs)


def split_host_port(value: str) -> tuple[str, int]:
    """Split ``host:port`` (IPv6 literals must use ``[::1]:port`` form)."""
    match = _HOST_PORT_RE.match(value.strip())
    if match is None:
        raise ConfigError(f"invalid 'host:port' address: {value!r}")
    host = match.group(1)
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    port = int(match.group(2))
    if not 1 <= port <= 65535:
        raise ConfigError(f"port out of range in {value!r}")
    if not host:
        raise ConfigError(f"empty host in {value!r}")
    return host, port


def _check_side(value: Any, key: str) -> str:
    if value not in ("local", "peer"):
        raise ConfigError(f"{key} must be 'local' or 'peer', got {value!r}")
    return value


def proxy_from_dict(raw: Any) -> ProxyConfig:
    """Build and validate a proxy from a mapping (config table or wire payload)."""
    if not isinstance(raw, dict):
        raise ConfigError("proxy entry must be a table/object")
    missing = [key for key in ("name", "listen", "listen_side", "backend", "backend_side") if key not in raw]
    if missing:
        raise ConfigError(f"proxy entry missing keys: {', '.join(missing)}")
    name = raw["name"]
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("proxy name must be a non-empty string")
    listen = raw["listen"]
    backend = raw["backend"]
    split_host_port(listen)
    split_host_port(backend)
    return ProxyConfig(
        name=name.strip(),
        listen=str(listen),
        listen_side=_check_side(raw["listen_side"], "listen_side"),
        backend=str(backend),
        backend_side=_check_side(raw["backend_side"], "backend_side"),
    )


def _parse_proxies(raw: Any) -> tuple[ProxyConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'proxies' must be a list of tables")
    proxies = tuple(proxy_from_dict(item) for item in raw)
    seen: set[str] = set()
    for proxy in proxies:
        if proxy.name in seen:
            raise ConfigError(f"duplicate proxy name: {proxy.name!r}")
        seen.add(proxy.name)
    return proxies


def _load_toml(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"failed to parse config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must be a TOML table")
    return data


def _parse_max_connections(raw: Any) -> int:
    if raw is None:
        return 0
    if not isinstance(raw, int) or raw < 0:
        raise ConfigError("'max_connections' must be a non-negative integer (0 = unlimited)")
    return raw


def _resolve_token(cli_val: str | None, data: dict[str, Any]) -> str | None:
    raw = data.get("token")
    return cli_val if cli_val is not None else (str(raw) if raw is not None else None)


def load_server_config(
    path: Path,
    *,
    listen: str | None = None,
    token: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> ServerConfig:
    data = _load_toml(path)
    listen = listen if listen is not None else str(data.get("listen", "0.0.0.0:7000"))
    split_host_port(listen)
    token = _resolve_token(token, data)
    tls_raw = data.get("tls")
    if tls_raw is None:
        tls_raw = {}
    if not isinstance(tls_raw, dict):
        raise ConfigError("'tls' must be a table with 'cert' and 'key'")
    tls_cert = tls_cert if tls_cert is not None else tls_raw.get("cert")
    tls_key = tls_key if tls_key is not None else tls_raw.get("key")
    if bool(tls_cert) != bool(tls_key):
        raise ConfigError("'tls.cert' and 'tls.key' must be set together")
    return ServerConfig(
        listen=listen,
        token=token,
        tls_cert=tls_cert,
        tls_key=tls_key,
        proxies=_parse_proxies(data.get("proxies")),
        max_connections=_parse_max_connections(data.get("max_connections")),
        allow_peer_backends=_parse_cidr_list(data.get("allow_peer_backends"), "allow_peer_backends"),
        allow_peer_listens=_parse_cidr_list(data.get("allow_peer_listens"), "allow_peer_listens"),
    )


def load_client_config(
    path: Path,
    *,
    server: str | None = None,
    token: str | None = None,
    tls: bool | None = None,
    tls_skip_verify: bool | None = None,
) -> ClientConfig:
    data = _load_toml(path)
    server = server if server is not None else str(data.get("server", "127.0.0.1:7000"))
    split_host_port(server)
    token = _resolve_token(token, data)
    tls = tls if tls is not None else bool(data.get("tls", False))
    tls_skip_verify = tls_skip_verify if tls_skip_verify is not None else bool(data.get("tls_skip_verify", False))
    return ClientConfig(
        server=server,
        token=token,
        tls=tls,
        tls_skip_verify=tls_skip_verify,
        proxies=_parse_proxies(data.get("proxies")),
        max_connections=_parse_max_connections(data.get("max_connections")),
        allow_peer_backends=_parse_cidr_list(data.get("allow_peer_backends"), "allow_peer_backends"),
        allow_peer_listens=_parse_cidr_list(data.get("allow_peer_listens"), "allow_peer_listens"),
    )
