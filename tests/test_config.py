import pytest

from websocket_tunnel.config import (
    ConfigError,
    ProxyConfig,
    _parse_allow_rules,
    is_endpoint_allowed,
    load_client_config,
    load_server_config,
    split_host_port,
)


def test_split_host_port():
    assert split_host_port("127.0.0.1:8080") == ("127.0.0.1", 8080)
    assert split_host_port("[::1]:7000") == ("::1", 7000)
    assert split_host_port("example.com:443") == ("example.com", 443)
    with pytest.raises(ConfigError):
        split_host_port("nope")
    with pytest.raises(ConfigError):
        split_host_port("127.0.0.1:0")
    with pytest.raises(ConfigError):
        split_host_port("127.0.0.1:65536")


def test_server_config(tmp_path):
    path = tmp_path / "server.toml"
    path.write_text(
        'listen = "127.0.0.1:7000"\n'
        'token = "sekret"\n'
        "\n"
        "[[proxies]]\n"
        'name = "db"\n'
        'listen = "127.0.0.1:3306"\n'
        'listen_side = "peer"\n'
        'backend = "192.168.1.5:3306"\n'
        'backend_side = "local"\n'
    )
    config = load_server_config(path)
    assert config.listen == "127.0.0.1:7000"
    assert config.token == "sekret"
    assert config.proxies == (
        ProxyConfig("db", "127.0.0.1:3306", "peer", "192.168.1.5:3306", "local"),
    )


def test_server_config_errors(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('listen = "127.0.0.1:7000"\n[[proxies]]\nname = "x"\n')
    with pytest.raises(ConfigError):
        load_server_config(path)

    path.write_text(
        'listen = "127.0.0.1:7000"\n'
        "[[proxies]]\n"
        'name = "x"\n'
        'listen = "a:b:c"\n'
        'listen_side = "up"\n'
        'backend = "127.0.0.1:1"\n'
        'backend_side = "local"\n'
    )
    with pytest.raises(ConfigError):
        load_server_config(path)

    path.write_text('listen = "127.0.0.1:7000"\ntls = { cert = "a.pem" }\n')
    with pytest.raises(ConfigError):
        load_server_config(path)


def test_duplicate_proxy_names(tmp_path):
    path = tmp_path / "dup.toml"
    path.write_text(
        'listen = "127.0.0.1:7000"\n'
        "[[proxies]]\n"
        'name = "x"\n'
        'listen = "127.0.0.1:1"\n'
        'listen_side = "local"\n'
        'backend = "127.0.0.1:2"\n'
        'backend_side = "peer"\n'
        "[[proxies]]\n"
        'name = "x"\n'
        'listen = "127.0.0.1:3"\n'
        'listen_side = "local"\n'
        'backend = "127.0.0.1:4"\n'
        'backend_side = "peer"\n'
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_server_config(path)


def test_is_endpoint_allowed():
    # Empty whitelist allows everything.
    assert is_endpoint_allowed("10.0.0.1", 80, ()) is True
    assert is_endpoint_allowed("1.2.3.4", 443, ()) is True

    # CIDR-only rules (no port restriction) allow any port.
    rules = _parse_allow_rules(["127.0.0.1/32", "10.0.0.0/8"], "x")
    assert is_endpoint_allowed("127.0.0.1", 22, rules) is True
    assert is_endpoint_allowed("10.1.2.3", 8080, rules) is True
    assert is_endpoint_allowed("192.168.1.1", 80, rules) is False

    # Non-IP hostnames are denied when a whitelist is active.
    assert is_endpoint_allowed("localhost", 80, rules) is False

    # IPv6
    rules6 = _parse_allow_rules(["::1/128"], "x")
    assert is_endpoint_allowed("::1", 80, rules6) is True
    assert is_endpoint_allowed("::2", 80, rules6) is False

    # Single-port rule.
    rules_p = _parse_allow_rules(["127.0.0.1/32:22"], "x")
    assert is_endpoint_allowed("127.0.0.1", 22, rules_p) is True
    assert is_endpoint_allowed("127.0.0.1", 23, rules_p) is False
    assert is_endpoint_allowed("10.0.0.1", 22, rules_p) is False

    # Port-range rule.
    rules_r = _parse_allow_rules(["10.0.0.0/8:8000-9000"], "x")
    assert is_endpoint_allowed("10.1.2.3", 8000, rules_r) is True
    assert is_endpoint_allowed("10.1.2.3", 9000, rules_r) is True
    assert is_endpoint_allowed("10.1.2.3", 7999, rules_r) is False
    assert is_endpoint_allowed("10.1.2.3", 9001, rules_r) is False
    assert is_endpoint_allowed("192.168.1.1", 8080, rules_r) is False

    # Comma-separated ports.
    rules_c = _parse_allow_rules(["127.0.0.1/32:8317,8319"], "x")
    assert is_endpoint_allowed("127.0.0.1", 8317, rules_c) is True
    assert is_endpoint_allowed("127.0.0.1", 8319, rules_c) is True
    assert is_endpoint_allowed("127.0.0.1", 8318, rules_c) is False

    # Mixed: comma + range.
    rules_m = _parse_allow_rules(["10.0.0.0/8:80,443,8000-8100"], "x")
    assert is_endpoint_allowed("10.0.0.1", 80, rules_m) is True
    assert is_endpoint_allowed("10.0.0.1", 443, rules_m) is True
    assert is_endpoint_allowed("10.0.0.1", 8050, rules_m) is True
    assert is_endpoint_allowed("10.0.0.1", 8101, rules_m) is False

    # Mixed rules: CIDR-only + port-restricted.
    mixed = _parse_allow_rules(["192.168.0.0/16", "127.0.0.1/32:2222"], "x")
    assert is_endpoint_allowed("192.168.1.1", 9999, mixed) is True
    assert is_endpoint_allowed("127.0.0.1", 2222, mixed) is True
    assert is_endpoint_allowed("127.0.0.1", 22, mixed) is False


def test_parse_allow_rules_errors(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('listen = "0.0.0.0:7000"\nallow_peer_backends = ["not-a-cidr"]\n')
    with pytest.raises(ConfigError, match="invalid CIDR"):
        load_server_config(path)

    path.write_text('listen = "0.0.0.0:7000"\nallow_peer_backends = "10.0.0.0/8"\n')
    with pytest.raises(ConfigError, match="must be a list"):
        load_server_config(path)

    # Invalid port range (start > end).
    path.write_text('listen = "0.0.0.0:7000"\nallow_peer_backends = ["127.0.0.1/32:9000-8000"]\n')
    with pytest.raises(ConfigError, match="invalid port range"):
        load_server_config(path)

    # Port out of range.
    path.write_text('listen = "0.0.0.0:7000"\nallow_peer_backends = ["127.0.0.1/32:0"]\n')
    with pytest.raises(ConfigError, match="invalid port"):
        load_server_config(path)


def test_max_connections_config(tmp_path):
    path = tmp_path / "s.toml"
    path.write_text('listen = "0.0.0.0:7000"\nmax_connections = 5\n')
    config = load_server_config(path)
    assert config.max_connections == 5

    # Default is 0 (unlimited).
    path.write_text('listen = "0.0.0.0:7000"\n')
    config = load_server_config(path)
    assert config.max_connections == 0

    # Negative value is rejected.
    path.write_text('listen = "0.0.0.0:7000"\nmax_connections = -1\n')
    with pytest.raises(ConfigError, match="non-negative"):
        load_server_config(path)


def test_client_config_overrides(tmp_path):
    path = tmp_path / "client.toml"
    path.write_text('server = "127.0.0.1:7000"\n')
    config = load_client_config(path)
    assert config.server == "127.0.0.1:7000"
    assert config.tls is False
    assert config.tls_skip_verify is False

    config = load_client_config(path, server="10.0.0.1:9999", token="t", tls=True, tls_skip_verify=True)
    assert config.server == "10.0.0.1:9999"
    assert config.token == "t"
    assert config.tls is True
    assert config.tls_skip_verify is True
