"""TOML configuration parsing and validation for both tunnel nodes."""

from __future__ import annotations

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


@dataclass(frozen=True)
class ClientConfig:
    server: str = "127.0.0.1:7000"
    token: str | None = None
    tls: bool = False
    tls_skip_verify: bool = False
    proxies: tuple[ProxyConfig, ...] = ()


def split_host_port(value: str) -> tuple[str, int]:
    """Split ``host:port`` (IPv6 literals must use ``[::1]:port`` form)."""
    if not isinstance(value, str):
        raise ConfigError(f"expected 'host:port', got {value!r}")
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


def load_server_config(
    path: Path,
    *,
    listen: str | None = None,
    token: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> ServerConfig:
    data = _load_toml(path)
    listen = listen if listen is not None else data.get("listen", "0.0.0.0:7000")
    split_host_port(listen)
    token = token if token is not None else data.get("token")
    if token is not None and not isinstance(token, str):
        raise ConfigError("'token' must be a string")
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
    server = server if server is not None else data.get("server", "127.0.0.1:7000")
    split_host_port(server)
    token = token if token is not None else data.get("token")
    if token is not None and not isinstance(token, str):
        raise ConfigError("'token' must be a string")
    tls = tls if tls is not None else bool(data.get("tls", False))
    tls_skip_verify = tls_skip_verify if tls_skip_verify is not None else bool(data.get("tls_skip_verify", False))
    return ClientConfig(
        server=server,
        token=token,
        tls=tls,
        tls_skip_verify=tls_skip_verify,
        proxies=_parse_proxies(data.get("proxies")),
    )
