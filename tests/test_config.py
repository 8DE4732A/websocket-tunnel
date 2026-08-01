import pytest

from websocket_tunnel.config import (
    ConfigError,
    ProxyConfig,
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
