import pytest

from websocket_tunnel.config import (
    ConfigError,
    ProxyConfig,
    is_host_allowed,
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


def test_is_host_allowed():
    # Empty whitelist allows everything.
    assert is_host_allowed("10.0.0.1", ()) is True
    assert is_host_allowed("1.2.3.4", ()) is True

    cidrs = ("127.0.0.1/32", "10.0.0.0/8")
    assert is_host_allowed("127.0.0.1", cidrs) is True
    assert is_host_allowed("10.1.2.3", cidrs) is True
    assert is_host_allowed("192.168.1.1", cidrs) is False

    # Non-IP hostnames are denied when a whitelist is active.
    assert is_host_allowed("localhost", cidrs) is False

    # IPv6
    assert is_host_allowed("::1", ("::1/128",)) is True
    assert is_host_allowed("::2", ("::1/128",)) is False


def test_parse_cidr_list_errors(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('listen = "0.0.0.0:7000"\nallow_peer_backends = ["not-a-cidr"]\n')
    with pytest.raises(ConfigError, match="invalid CIDR"):
        load_server_config(path)

    path.write_text('listen = "0.0.0.0:7000"\nallow_peer_backends = "10.0.0.0/8"\n')
    with pytest.raises(ConfigError, match="must be a list"):
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
