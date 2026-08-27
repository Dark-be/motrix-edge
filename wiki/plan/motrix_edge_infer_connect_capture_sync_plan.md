# Infer 连接显式化 + Capture 元信息同步实施计划

## 摘要

-   **infer**：进入推理会话**不再自动连接**策略服务器；新增 `infer connect` 命令**单次尝试**
    连接推理节点，`infer rollout` 仅在已连接时可用（未连接 → 503）。
-   **capture**：采集会话期间周期向机器人进程查询 **capture status**（含采集员姓名 / 任务
    名称等元信息）；新增 `capture sync --meta <json>` 把元信息从 console / web 同步到机器人
    进程，进程保存一轮数据时附加。

## TODO

-   [x] 命令注册：`infer connect` / `capture sync`（含 `--meta` JSON 解析 `parse_meta`）
-   [x] adapter 契约：`CaptureStatus` / `capture_status()` / `sync_capture_meta()` +
        HTTP 端点（`/v1/capture/status` / `/v1/capture/sync`）+ TestRobotAdapter /
        DualPiperAdapter / FakeRobotAdapter 实现
-   [x] InferSession：去掉 `run()` 自动连接；`infer connect` 显式连接；rollout 未连接 503；
        连接状态暴露（`connected`）
-   [x] CaptureSession：消费 `capture sync`；node 周期刷新 `capture_status` 缓存（ACTIVE+capture）
-   [x] server：`POST /v1/infers/connect`、`POST /v1/captures/sync` + `/v1/commands`
        capability（`infer_connect` / `capture_sync`）
-   [x] 单元测试（infer connect / capture sync / status 上报）+ 容器内 `uv run` 校验
