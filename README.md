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

也可以直接从 PyPI 安装：

```bash
uv tool install websocket-tunnel
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

### server.toml

```toml
listen = "0.0.0.0:7000"          # 控制端口（ws/wss）
token = "secret"                  # 可选，共享认证 token
tls = { cert = "server.crt", key = "server.key" }   # 可选，启用 wss

# 最大并发控制连接数（0 = 不限）
max_connections = 10

# 允许 client 请求本节点连接的 backend 地址范围（空 = 允许所有）
# 格式：CIDR、CIDR:port、CIDR:start-end、CIDR:port,port,start-end
allow_peer_backends = [
    "127.0.0.1/32:8000-9000",     # 本机 8000~9000 端口
    "10.0.0.0/8",                 # 内网所有端口
]

# 允许 client 请求本节点绑定的 listen 地址范围（空 = 允许所有）
allow_peer_listens = [
    "0.0.0.0/0:9000-9100",        # 仅允许绑定 9000~9100
]

[[proxies]]
name = "web"
listen = "0.0.0.0:8080"
listen_side = "local"             # "local" = 本节点, "peer" = 对端节点
backend = "127.0.0.1:3000"
backend_side = "peer"
```

### client.toml

```toml
server = "example.com:443"        # 服务端地址（域名或 IP）
token = "secret"
tls = true                        # 连接 wss 时设为 true
tls_skip_verify = false           # 自签证书时设为 true

# 允许服务端请求本节点连接的 backend 地址范围（空 = 允许所有）
allow_peer_backends = ["127.0.0.1/32"]

# 允许服务端请求本节点绑定的 listen 地址范围（空 = 允许所有）
allow_peer_listens = ["127.0.0.1/32"]

[[proxies]]
name = "web"
listen = "0.0.0.0:8080"
listen_side = "peer"
backend = "127.0.0.1:3000"
backend_side = "local"
```

### 端口规则格式

`allow_peer_backends` 和 `allow_peer_listens` 中每条规则的端口部分支持以下写法，
可任意组合（逗号分隔）：

| 写法 | 含义 |
| --- | --- |
| `"10.0.0.0/8"` | 该网段所有端口 |
| `"127.0.0.1/32:22"` | 仅端口 22 |
| `"127.0.0.1/32:8000-9000"` | 8000～9000（含两端） |
| `"127.0.0.1/32:22,8317,8319"` | 端口 22、8317、8319 |
| `"10.0.0.0/8:80,443,8000-8100"` | 逗号与范围混用 |

白名单中的地址必须使用 IP（不支持主机名）；CIDR 掩码可省略，如 `"10.0.0.1"` 等同于 `"10.0.0.1/32"`。

## 通过 Cloudflare Tunnel 部署

服务端可隐藏在 Cloudflare Tunnel 后面，无需开放公网端口：

1. 服务端本机运行 `cloudflared`，`service` 配置为 `http://127.0.0.1:7000`（必须用 `http://`，不能用 `tcp://`）
2. 客户端连接时使用域名 + 443 端口，并开启 TLS：

```toml
server = "wtunnel.example.com:443"
tls = true
```

Cloudflare 会自动代理 WebSocket Upgrade，wtunnel 两端无需额外配置。
内置的应用层 PING（每 30 秒）保证连接不被 Cloudflare 的 100 秒空闲超时断开。

## 传输与协议概要

- 每对节点一条 WebSocket 长连接（client 主动连接 server），控制消息与所有数据流
  在其上多路复用；每条 TCP 流分配独立 stream id。
- 二进制帧：首字节为消息类型；控制消息为 JSON，数据帧为
  `stream_id`(4 字节大端) + 数据块（默认 32 KiB）。
- 支持半关闭：任一方向 EOF 只关闭对应方向，保证 HTTP/1.0 与 keep-alive 响应完整。
- 握手采用 HMAC-SHA256 challenge-response：服务端发送随机 nonce，客户端以
  `HMAC-SHA256(token, nonce)` 响应，token 本身不在网络上传输，每次握手 nonce
  不同，抓包重放无效。
- client 断线自动重连（指数退避 1s→30s），重连后双方重新注册代理并重新绑定监听。

## 开发与测试

```bash
uv run pytest          # 单元 + 集成测试
uv build               # 构建发布产物
```

## 发布到 PyPI

打上 `v` 开头的 tag 并推送即触发 GitHub Actions 发布：

```bash
git tag v0.3.0
git push origin v0.3.0
```

工作流（`.github/workflows/release.yml`）先执行 `uv build`（此时 git 工作区干净，
hatch-vcs 读取精确 tag 版本），再运行测试，最后通过 trusted publishing（OIDC）
发布到 PyPI。首次使用前需要在 PyPI 项目页配置 Trusted Publisher：仓库
`8DE4732A/websocket-tunnel`、工作流名 `Publish to PyPI`。若改用 API token，删除
workflow 中 `id-token: write` 权限，并配置 `PYPI_TOKEN` secret 传给 `uv publish`。

## 安全说明

**认证**：握手采用 HMAC-SHA256 challenge-response，token 不在网络上明文传输，
每次握手的 nonce 唯一，抓包重放无法通过验证。公网部署请务必设置足够随机的 token
（建议 32 字节以上）。

**传输加密**：公网部署请务必启用 wss（服务端配置 `tls.cert` / `tls.key`，或通过
Cloudflare Tunnel 等反向代理终止 TLS）。`tls_skip_verify = true` 仅适用于受控的
自签证书环境，生产环境应使用受信任 CA 签发的证书。

**连接数限制**：服务端 `max_connections` 防止恶意客户端通过大量控制连接耗尽资源，
建议生产部署时设置合理上限（如 `10`）。

**后端 / 监听白名单**：默认配置下，持有有效 token 的对端可请求本节点连接任意 IP
或绑定任意端口。若部署于半信任网络，应配置 `allow_peer_backends` 和
`allow_peer_listens` 将出站连接与监听绑定限定在必要范围，支持精确到端口级别的
控制（如 `"127.0.0.1/32:8317,8319"`），以防内网探测（SSRF）与端口滥用。

v1 暂不提供 mTLS、证书固定与配置热加载。
