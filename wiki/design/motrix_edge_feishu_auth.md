# 飞书登录与可信采集员身份

## 摘要

Edge Web Console 通过飞书 OAuth 登录识别采集员。飞书凭证只保存在 Edge 服务端内存会话中，
浏览器仅持有不透明的 HttpOnly Cookie；采集元信息同步时，服务端根据登录会话覆盖
`operator` 与 `operator_identity`，并校验登录主体和 Edge 租约主体一致。未配置飞书认证时，
现有 `/v1/captures/sync` 行为保持兼容。

## 目标与边界

-   飞书是采集员身份提供方，姓名只用于展示，稳定的 `subject` 用于租约和审计。
-   浏览器不能提交可信身份；它只提交任务名、标签等业务元信息。
-   access token、refresh token、authorization code、Cookie 和 app secret 不进入采集 JSON。
-   `EdgeNode` 与 adapter 继续把 `meta` 当作通用 JSON 对象透传，不感知 OAuth。
-   当前 Edge 单进程部署使用内存登录事务和会话；多进程部署必须把两者迁移到共享存储。

## 登录流程

1. 前端访问 `GET /v1/auth/feishu/login?return_to=/capture`。
2. Edge 生成一次性 `state`，保存受限的站内返回路径，并重定向到飞书授权页。
3. 飞书携带 `code` 和 `state` 回调 `GET /v1/auth/feishu/callback`。
4. Edge 校验并一次性消费 `state`，以 `code` 换取 user access token，再读取当前用户信息。
5. Edge 丢弃 OAuth token，建立有期限的不透明登录会话，通过 HttpOnly Cookie 返回浏览器。
6. 前端用 `GET /v1/auth/me` 获取展示身份；`POST /v1/auth/logout` 删除会话。

`return_to` 只接受以单个 `/` 开头的站内路径；最终跳转固定拼接配置的 `frontend_url`，避免开放重定向。

## 采集同步

启用飞书认证后，`POST /v1/captures/sync` 依次执行：

1. 校验登录 Cookie；
2. 校验 `X-Lease-Id` 对应活跃租约；
3. 要求租约 `holder_subject_id` 等于登录用户的 `subject`；
4. 删除调用方提交的 `operator`、`operator_identity` 和 `capture_context`；
5. 注入服务端生成的身份和审计上下文，再走现有 `capture sync` 命令。

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

飞书认证配置位于 `edge.yml` 的 `auth.feishu`，包内默认开启。`app_id` / `app_secret` 默认分别通过
`app_id_env` / `app_secret_env` 指定的环境变量读取；缺少必填部署值时启动会直接报出缺失项，避免静默
退化成可伪造采集员的匿名模式。Cookie 模式下 CORS 不能使用通配 origin，必须配置明确的
`allowed_origins`；生产环境同时替换回调与前端 URL、启用 HTTPS 和 Secure Cookie。只有显式配置
`enabled: false` 才恢复原有调用方直接提交 `operator` 的兼容行为。
