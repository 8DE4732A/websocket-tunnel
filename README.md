# websocket-tunnel

一个类似 frp 的内网穿透工具：Python + uv 实现，传输层使用 WebSocket 多路复用，
支持任意 TCP 服务（HTTP/HTTPS/SSH/MySQL 等）的字节流透传。构建产物通过
`uv build` 生成，安装后提供 `wtunnel` CLI，分为 `server` 与 `client` 两种模式。

与 frp 的主要差异：**代理不区分服务端/客户端**。代理条目可以声明在任意节点的
配置中，监听端（listen）与后端（backend）分别用 `local` / `peer` 指定在哪一侧，
因此以下四种组合全部支持：

| listen 侧 | backend 侧 | 用途 |
| --- | --- | --- |
| server | client | 经典 frp：把内网服务暴露到公网服务器端口 |
| client | server | 反向：把服务端网络里的服务映射到客户端本地端口 |
| server | server | 服务端本机端口转发（不占用隧道） |
| client | client | 客户端本机端口转发（不占用隧道） |

## 安装

要求 Python >= 3.11 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync            # 开发环境（含测试依赖）
uv build           # 构建 sdist + wheel
uv run wtunnel --version
```

也可以把 wheel 安装到任意环境：

```bash
uv pip install dist/websocket_tunnel-0.1.0-py3-none-any.whl
wtunnel --version
```

## 快速开始

### 1. 启动服务端

```bash
wtunnel server -c examples/server.toml
```

### 2. 启动客户端

```bash
wtunnel client -c examples/client.toml
```

客户端默认把本机 `127.0.0.1:3000` 暴露到服务端的 `0.0.0.0:8080`，访问
`http://<server>:8080` 即可到达客户端内网的服务。

## CLI 参数

```text
wtunnel server -c server.toml [--listen HOST:PORT] [--token TOKEN]
                       [--tls-cert PATH --tls-key PATH] [-v]
wtunnel client -c client.toml [--server HOST:PORT] [--token TOKEN]
                       [--tls] [--tls-skip-verify] [-v]
```

CLI 参数覆盖配置文件中的同名项；`-v` 输出 DEBUG 日志。

## 配置参考

`server.toml`：

```toml
listen = "0.0.0.0:7000"          # 控制端口（ws/wss）
token = "secret"                 # 可选，共享认证 token
tls = { cert = "server.crt", key = "server.key" }   # 可选，启用 wss

[[proxies]]
name = "web"
listen = "0.0.0.0:8080"
listen_side = "local"            # "local" | "peer"
backend = "127.0.0.1:3000"
backend_side = "peer"
```

`client.toml`：

```toml
server = "127.0.0.1:7000"        # 服务端地址
token = "secret"
tls = false                      # true 时使用 wss
tls_skip_verify = false          # 自签证书时设为 true

[[proxies]]
name = "web"
listen = "0.0.0.0:8080"
listen_side = "peer"
backend = "127.0.0.1:3000"
backend_side = "local"
```

`listen` / `backend` 均为 `host:port`，IPv6 使用 `[::1]:8080` 形式。backend 可以
指向任意可达地址（含内网 IP 的外部服务端口），不限于 localhost。

## 传输与协议概要

- 每对节点一条 WebSocket 长连接（client 主动连接 server），控制消息与所有数据流
  在其上多路复用；每条 TCP 流分配独立 stream id。
- 二进制帧：首字节为消息类型；控制消息为 JSON，数据帧为
  `stream_id`(4 字节大端) + 数据块（默认 32 KiB）。
- 支持半关闭：任一方向 EOF 只关闭对应方向，保证 HTTP/1.0 与 keep-alive 响应完整。
- 握手时校验共享 token（失败即断开）；服务端可选 wss，客户端可跳过证书校验。
- client 断线自动重连（指数退避 1s→30s），重连后双方重新注册代理并重新绑定监听。

## 开发与测试

```bash
uv run pytest          # 单元 + 集成测试
uv build               # 构建发布产物
```

## 发布到 PyPI

打上 `v` 开头的 tag 并推送即触发 GitHub Actions 发布：

```bash
git tag v0.1.0
git push origin v0.1.0
```

工作流（`.github/workflows/release.yml`）会校验 tag 与包版本一致、运行测试、
`uv build` 构建后通过 trusted publishing（OIDC）发布到 PyPI。首次使用前需要在
PyPI 项目页配置 Trusted Publisher：仓库 `8DE4732A/websocket-tunnel`、
工作流名 `Publish to PyPI`。若改用 API token，删除 workflow 中 `id-token: write`
权限，并配置 `PYPI_TOKEN` secret 传给 `uv publish`。

## 安全说明

共享 token 即信任边界：持有 token 的 client 可以请求服务端绑定任意监听端口。
公网部署请务必启用 wss（或搭配其他加密隧道）并妥善保管 token。v1 暂不提供
mTLS、证书固定与配置热加载。
