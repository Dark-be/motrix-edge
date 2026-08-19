# 设备身份（identity）

## 摘要

`identity/` 提供 Edge **本地设备身份声明**（引用，非权威；权威在 MotrixConsole）。Edge 只持有
本地身份声明，用于随请求上报与本地校验，不产出、不裁决注册 / Capability 等状态。

## 契约

-   `Identity`（frozen dataclass）：`edge_id` / `edge_name` / `edge_version`，均来自部署时配置
    `identity` 段，Edge 只读。
-   `Identity.headers()`：**预留发送接口** —— 序列化为请求头 / 元数据：

    ```python
    {
        "X-Edge-Id": edge_id,
        "X-Edge-Name": edge_name,
        "X-Edge-Version": edge_version,
    }
    ```

    具体发送（真正的 HTTP 调用）由 HTTP 服务 / 客户端层实现；当前 `GET /v1/health` 经
    `identity.headers()` 上报身份概要。

-   `load_identity(base_cfg)`：从配置 `identity` 段加载（缺省占位值，便于无配置联调）。
-   `new_correlation_id()` / `new_idempotency_key()`：跨产品请求元数据生成器（链路追踪 / 幂等）。

## 配置

```yaml
identity:
    edge_id: edge-test-001
    edge_name: edge-test
    edge_version: "0.1.0"
```

## 相关文档

-   HTTP 上报：[HTTP 控制面（server）](./motrix_edge_server.md)
-   配置段：[配置与命令行](./motrix_edge_config.md)
-   代码入口：`src/motrix_edge/identity/` —— 随 **feat/3**（HTTP 控制面）落地
