"""TOML configuration parsing and validation for both tunnel nodes."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HOST_PORT_RE = re.compile(r"^(\[[^\[\]]+\]|[^:]+):([0-9]+)$")
# Matches optional ":port" or ":start-end" suffix on an allow-rule string.
_RULE_PORT_RE = re.compile(r"^(.+):(\d+)(?:-(\d+))?$")


class ConfigError(Exception):
    """Raised when a configuration file or value is invalid."""


@dataclass(frozen=True)
class AllowRule:
    """A single entry in allow_peer_backends / allow_peer_listens.

    ``cidr``       — an IP network (e.g. ``127.0.0.1/32``, ``10.0.0.0/8``).
    ``port_min``   — lower bound of allowed ports (1 when absent).
    ``port_max``   — upper bound of allowed ports (65535 when absent).

    The original string form ``"cidr[:port[-port]]"`` is preserved in
    ``raw`` for error messages and round-tripping.
    """

    cidr: str
    port_min: int
    port_max: int
    raw: str


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
    # Rules controlling which backends a peer may ask this node to reach.
    # Empty list = allow all (default for backward compatibility).
    allow_peer_backends: tuple[AllowRule, ...] = ()
    # Rules controlling which listen addresses a peer may ask this node to bind.
    allow_peer_listens: tuple[AllowRule, ...] = ()


@dataclass(frozen=True)
class ClientConfig:
    server: str = "127.0.0.1:7000"
    token: str | None = None
    tls: bool = False
    tls_skip_verify: bool = False
    proxies: tuple[ProxyConfig, ...] = ()
    max_connections: int = 0
    allow_peer_backends: tuple[AllowRule, ...] = ()
    allow_peer_listens: tuple[AllowRule, ...] = ()


def _parse_allow_rule(item: str, key: str) -> AllowRule:
    """Parse one allow-rule string into an AllowRule.

    Accepted formats (backward-compatible):
      ``"10.0.0.0/8"``          — CIDR only, all ports allowed
      ``"127.0.0.1/32:22"``     — CIDR + single port
      ``"127.0.0.1/32:8000-9000"`` — CIDR + port range
    IPv6 CIDRs must NOT use bracket notation here; they are plain strings
    (e.g. ``"::1/128"`` or ``"::1/128:80"``).
    """
    if not isinstance(item, str):
        raise ConfigError(f"'{key}' entries must be strings, got {item!r}")

    # Try to split off a trailing :port or :start-end.
    # We try the longest possible CIDR match first: if the string contains
    # a port suffix it will have at least two colons (IPv4) or be IPv6.
    port_min, port_max = 1, 65535
    cidr_part = item

    m = _RULE_PORT_RE.match(item)
    if m:
        candidate_cidr = m.group(1)
        try:
            ipaddress.ip_network(candidate_cidr, strict=False)
            # Valid CIDR before the port suffix — parse the port range.
            p_start = int(m.group(2))
            p_end = int(m.group(3)) if m.group(3) is not None else p_start
            if not (1 <= p_start <= 65535 and 1 <= p_end <= 65535 and p_start <= p_end):
                raise ConfigError(f"invalid port range in '{key}': {item!r}")
            cidr_part = candidate_cidr
            port_min, port_max = p_start, p_end
        except ValueError:
            # The regex matched but the prefix isn't a valid CIDR —
            # fall through and validate the whole string as a plain CIDR.
            pass

    try:
        ipaddress.ip_network(cidr_part, strict=False)
    except ValueError as exc:
        raise ConfigError(f"invalid CIDR in '{key}': {item!r} — {exc}") from exc

    return AllowRule(cidr=cidr_part, port_min=port_min, port_max=port_max, raw=item)


def _parse_allow_rules(raw: Any, key: str) -> tuple[AllowRule, ...]:
    """Validate a list of allow-rule strings from config."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"'{key}' must be a list of strings")
    return tuple(_parse_allow_rule(item, key) for item in raw)


def is_endpoint_allowed(host: str, port: int, rules: tuple[AllowRule, ...]) -> bool:
    """Return True when ``host:port`` matches any rule.

    Empty *rules* means "allow all" (backward-compatible default).
    Non-IP hostnames are always denied when a whitelist is active.
    """
    if not rules:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(
        addr in ipaddress.ip_network(r.cidr, strict=False) and r.port_min <= port <= r.port_max
        for r in rules
    )


# Backward-compatible alias for callers that only check the host.
# With port-restricted rules port=0 never falls in any valid range,
# so callers that need port checking must use is_endpoint_allowed directly.
def is_host_allowed(host: str, rules: tuple[AllowRule, ...]) -> bool:
    """Deprecated alias; use is_endpoint_allowed with an explicit port."""
    if not rules:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(r.cidr, strict=False) for r in rules)


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
        allow_peer_backends=_parse_allow_rules(data.get("allow_peer_backends"), "allow_peer_backends"),
        allow_peer_listens=_parse_allow_rules(data.get("allow_peer_listens"), "allow_peer_listens"),
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
        allow_peer_backends=_parse_allow_rules(data.get("allow_peer_backends"), "allow_peer_backends"),
        allow_peer_listens=_parse_allow_rules(data.get("allow_peer_listens"), "allow_peer_listens"),
    )
