# Motrix Console BFF

## 摘要

`console/` 是正式子项目：同一个 Node 进程托管 React 构建产物、飞书 OAuth、HttpOnly 登录会话、
静态 Edge 注册表和浏览器到 Edge 的 HTTP 代理。分布式 Edge 不保存飞书 App Secret，也不接收动态
OAuth 回调；Console 使用每台 Edge 独立的共享密钥签署用户身份，Edge 验证签名后将可信采集员写入
采集元信息。

## 目标与边界

-   飞书只登记一个稳定回调地址，属于固定 Console 域名。
-   浏览器只持有 Console 的 HttpOnly Cookie，不把飞书 token 或代理签名密钥暴露给 JavaScript。
-   Edge 地址只能来自服务端静态注册表；浏览器不能提交任意代理目标，避免 SSRF。固定可信局域网可
    为单台设备显式启用 `trustedNetwork`，不配置 HMAC，但必须用网络 ACL 限制 Edge 入口。
-   Console 不取代 Edge 租约和状态机；受控请求仍携带原 `X-Lease-Id`，Edge 作最终校验。
-   Console 只在已登录后代理 Edge API；代理时附加签名身份，但不信任浏览器提交的 `operator`。
-   `http://localhost:8000` 是浏览器内建的本机直连目标，不经过 BFF、无需出现在静态注册表；请求不携带
    Console Cookie，适用于 Console 与 Edge 同机的开发 / 现场调试。
-   首版代理普通 HTTP API 与 WebRTC offer 信令；WebRTC 媒体仍由浏览器和 Edge 直接协商。

## 拓扑

```text
Browser
  │  Cookie / same origin
  ▼
console/server (Node, fixed host)
  ├─ /api/auth/*
  ├─ /api/edges
  └─ /api/edges/{edge_id}/v1/*
                 │ HMAC signed identity + X-Lease-Id
                 ▼
              Edge A/B/C
```

## 配置

Console 从 `console/console.config.json` 读取非敏感配置，Secret 只从环境变量读取：

```json
{
    "host": "0.0.0.0",
    "port": 5174,
    "frontendUrl": "https://console.example.com",
    "redirectUri": "https://console.example.com/api/auth/feishu/callback",
    "cookieSecure": true,
    "allowedTenants": ["tenant_key_1"],
    "edges": {
        "piper-a": {
            "label": "双臂 Piper A",
            "baseUrl": "http://10.30.20.80:8000",
            "sharedSecretEnv": "MOTRIX_CONSOLE_EDGE_PIPER_A_SECRET"
        }
    }
}
```

`allowedTenants` 非空时只允许列出的飞书 `tenant_key` 登录；空列表仅用于不限制租户的开发环境。
环境变量 `MOTRIX_CONSOLE_FEISHU_ALLOWED_TENANTS` 可用逗号分隔列表覆盖该配置，生产环境应显式设置。

每台 Edge 只配置对应共享密钥：

```yaml
auth:
    console_proxy:
        enabled: true
        shared_secret_env: MOTRIX_EDGE_CONSOLE_SHARED_SECRET
        max_clock_skew_s: 30
```

Console 与该 Edge 的两个环境变量值必须相同。

### 可信局域网模式

实验室固定 IP 设备可在单条静态注册项设置 `"trustedNetwork": true` 并省略 `sharedSecretEnv`。该模式
仍要求浏览器先登录飞书，且 Console 在转发 `POST /v1/captures/sync` 前删除浏览器提交的
`operator` / `operator_identity` / `capture_context`，用服务端登录 Session 重建采集身份。但是 Edge
不再能独立验证请求是否来自 Console，因此部署必须通过主机防火墙、交换机 ACL 或隔离管理网保证 Edge
控制端口只接受 Console 主机。`trustedNetwork` 与 `sharedSecretEnv` 互斥；非固定可信网络必须使用
HMAC 模式。

## 签名契约

Console 为每个代理请求生成：

-   `X-Motrix-Subject`：规范飞书主体；
-   `X-Motrix-Name`：展示名；
-   `X-Motrix-Timestamp`：Unix 秒；
-   `X-Motrix-Nonce`：一次性随机值；
-   `X-Motrix-Signature`：HMAC-SHA256。
-   `X-Motrix-Identity`：Base64URL 编码的完整飞书身份 claims。

签名输入依次包含版本、时间、nonce、HTTP 方法、原始路径、body SHA-256、subject、display name、
完整身份 claims。
Edge 校验时间窗口、常量时间签名比较和 nonce 未使用；同一 nonce 在窗口内重放会被拒绝。

## 采集身份

`POST /v1/captures/sync` 在启用 Console proxy 验证时必须带有效签名。Edge 要求签名 subject 与
活跃租约 `holder_subject_id` 相同，再覆盖 `operator` / `operator_identity` / `capture_context`。
浏览器 body 中的同名字段永远不作为可信事实。

## 运行

先构建并启动 Node Console，再分别启动 Edge：

```bash
cd console
npm ci
npm run build
node --env-file=../.env server/index.mjs

uv run --env-file .env motrix-edge run --config /etc/motrix-edge/edge.yml
```
