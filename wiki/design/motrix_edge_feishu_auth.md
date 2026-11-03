# 飞书登录与可信采集员身份

## 摘要

固定部署的 Motrix Console 通过飞书 OAuth 识别采集员。飞书凭证只保存在 Console 内存会话中，
浏览器仅持有 Console 的不透明 HttpOnly Cookie。HMAC 模式由 Console 签署身份、Edge 验签并覆盖
`operator` / `operator_identity`；固定可信局域网模式则由 Console 在转发前覆盖身份，Edge 只接受受限
管理网入口。
完整拓扑见 [Motrix Console BFF](./motrix_console_bff.md)。Edge 自带 OAuth 仅保留为单机调试兼容面。

## 目标与边界

-   飞书是采集员身份提供方，姓名只用于展示，稳定的 `subject` 用于租约和审计。
-   浏览器不能提交可信身份；它只提交任务名、标签等业务元信息。
-   access token、refresh token、authorization code、Cookie 和 app secret 不进入采集 JSON。
-   `EdgeNode` 与 adapter 继续把 `meta` 当作通用 JSON 对象透传，不感知 OAuth。
-   当前 Console 单进程部署使用内存登录事务和会话；多进程部署必须把两者迁移到共享存储。

## 登录流程

1. 前端访问 `GET /api/auth/feishu/login?return_to=/capture`。
2. Console 生成一次性 `state`，保存受限的站内返回路径，并重定向到飞书授权页。
3. 飞书携带 `code` 和 `state` 回调 `GET /api/auth/feishu/callback`。
4. Console 校验并一次性消费 `state`，以 `code` 换取 user access token，再读取当前用户信息。
5. Console 丢弃 OAuth token，建立有期限的不透明登录会话，通过 HttpOnly Cookie 返回浏览器。
6. 前端用 `GET /api/auth/me` 获取展示身份；`POST /api/auth/logout` 删除会话。

`return_to` 只接受以单个 `/` 开头的站内路径；最终跳转固定拼接配置的 `frontend_url`，避免开放重定向。

## 采集同步

HMAC 模式下，Console 代理的 `POST /v1/captures/sync` 依次执行：

1. Console 校验登录 Cookie，并对方法 / 路径 / body hash / 用户 / 时间 / nonce 作 HMAC 签名；
2. Edge 验证时间窗、签名与 nonce 未重放；
3. Edge 校验 `X-Lease-Id` 对应活跃租约；
4. 要求租约 `holder_subject_id` 等于签名用户的 `subject`；
5. 删除调用方提交的身份字段，注入可信身份和审计上下文，再走现有 `capture sync` 命令。

固定可信局域网模式不配置 HMAC：Console 校验登录 Cookie 后先删除 body 中的身份字段，再从服务端
Session 注入操作员身份并转发；Edge 仍校验租约 ID，但不能独立证明身份来源。该模式只允许用于静态 IP
白名单，并必须在网络层把 Edge 控制端口限制为 Console 主机或可信管理网。

同步必须发生在 `capture episode start` 之前，因为机器人进程在开始录制时快照本轮元信息。

## 元信息契约

```json
{
    "meta_schema_version": 2,
    "operator": "张三",
    "operator_identity": {
        "provider": "feishu",
        "subject": "feishu:t_xxx:ou_xxx",
        "tenant_key": "t_xxx",
        "open_id": "ou_xxx",
        "union_id": "on_xxx",
        "user_id": "u_xxx",
        "display_name": "张三"
    },
    "task_name": "桌面前移",
    "capture_context": {
        "source": "web_console",
        "lease_id": "lease_xxx",
        "authenticated_at": "2026-11-03T10:20:30+08:00",
        "synced_at": "2026-11-03T10:22:05+08:00"
    }
}
```

`operator` 保留为兼容字段；身份判断一律使用 `operator_identity.subject`。可选 ID 缺失时不写
`null` 键。上传扫描把三个新增字段识别为结构化元信息，原始 JSON 仍完整保留。

## 配置与部署

飞书 App ID / Secret 位于固定主机环境变量，OAuth URI 与 Edge 注册表位于
`console/console.config.json`，不再分发到 Edge。HMAC 设备通过双方的 `shared_secret_env` 配置同一份
设备级共享密钥；固定可信局域网设备显式写 `trustedNetwork: true`，不保存设备系统密码或代理密钥。
生产环境必须使用 HTTPS 与 Secure Cookie，并限制 Edge 网络入口。`edge.yml.auth.feishu` 缺省关闭，
仅用于浏览器、前端和 Edge 确实同机的单机调试。
