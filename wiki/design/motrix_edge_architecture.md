# 边缘节点架构（motrix_edge）总览

## 摘要

`motrix_edge` 是对现有机器人的二次抽象：边缘节点作为**任务运行时**，负责收集 observation 并
**主动请求**推理节点（Endpoint）返回 action，本地校验后限速执行；任务生命周期由控制面
（Console）驱动。本文是**总览与导航**，定义分层与数据流；各子包的详细设计见下方「按包索引」。

> **定位与边界**：本文只覆盖**任务运行时**层。MotrixLoop M11/M12 定义的 Edge 核心能力
> （device identity 权威、面向 Console 的完整 HTTP/WebRTC、Capability 校验与 Safety Guard、
> Process Supervisor、CaptureBundle → Spool → Uploader、Rollout Runtime 等）由 MotrixConsole
> 控制面与 Edge 平台层负责，不在本文范围。

## 目标与成功标准

-   提供统一的边缘节点运行时接口，供数采、推理、真机强化学习使用。
-   数据流遵循信任模型：Edge 主动调用 Endpoint → Endpoint 只返回受限 action → Edge 本地校验后限速执行。
-   只有 Console 控制面驱动任务生命周期；推理节点不向 Edge 任意发指令。

**成功标准**：新增任务类型只需新增会话类；新增硬件只需实现 `RobotAdapter` 并注册 entry point；
命令源可替换（CLI ↔ HTTP）；纯逻辑可在无硬件环境用 Fake 测试。

## 整体原则

1. **分层**：节点（生命周期）→ 会话（任务流程）→ 硬件适配（RobotAdapter）。
2. **注册表工厂**：`get_adapter` / `get_session` / `get_policy` 按配置或命令选择性实例化，均懒加载。
3. **命令驱动**：节点与会话只响应命名命令（`Command`），不绑定输入来源；`command_source` 可注入。
4. **契约优先**：基类以抽象方法定义接口，子类实现具体逻辑；核心不依赖具体实现。

## 按包索引（分包导航）

文档按 `src/motrix_edge/` 子包组织，每包一篇：

| 包                  | 职责                                            | 设计文档                                                     |
| ------------------- | ----------------------------------------------- | ------------------------------------------------------------ |
| `__main__.py`       | CLI 入口（run / adapters / version）            | [配置与命令行](./motrix_edge_config.md)                      |
| `node.py`           | EdgeNode 生命周期状态机、命令分发、周期任务     | [节点生命周期（node）](./motrix_edge_node.md)                |
| `adapter/`          | RobotAdapter HAL、discover、工厂、HTTP/SHM 契约 | [机器人适配器（adapter）](./motrix_edge_adapter.md)          |
| `session/`          | 会话（Capture / Infer）与工厂                   | [会话（session）](./motrix_edge_session.md)                  |
| `policy/`           | 推理策略客户端（openpi 等）                     | [推理策略客户端（policy）](./motrix_edge_policy.md)          |
| `frame/`            | FrameManager 观测帧缓存                         | [FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md) |
| `server/`           | FastAPI HTTP 控制面（/v1/\*）                   | [HTTP 控制面（server）](./motrix_edge_server.md)             |
| `identity/`         | Edge 本地设备身份声明                           | [设备身份（identity）](./motrix_edge_identity.md)            |
| `lease/`            | Edge 级租约机制                                 | [Edge 级租约（lease）](./motrix_edge_lease.md)               |
| `utils/commands.py` | 命令模型 / 解析 / 传输（CommandBus）            | [命令总线（CommandBus）](./motrix_edge_command_bus.md)       |
| `config/`           | 全局路径常量 + yaml 配置                        | [配置与命令行](./motrix_edge_config.md)                      |

## 拓扑与数据流

```
Console / 浏览器 ──HTTP──▶ server（web 线程）──CommandBus.submit──▶ EdgeNode（主线程）
                                                                     │
        CLI 键盘 ──CommandBus.push───────────────────────────────────┤
                                                                     ▼
                              session（Capture / Infer）──RobotAdapter──▶ 机器人进程
                                                                     │
                              policy（InferSession 内）──ws──▶ 推理节点（Endpoint）
```

-   **启动**：`motrix-edge run` = node 主线程持续运行 `EdgeNode`（CLI 按键保留）+ web 作为独立线程跑 FastAPI；web 经共享 `CommandBus` 驱动 node，不持有 node。
-   **adapter 生命周期归节点**：IDLE 探测绑定 → READY → 任务间保持 → ERROR 恢复释放；会话复用注入的 adapter。
-   **会话选定即进入 ACTIVE**（`session run <type>` 一步完成）；`session quit` 退出回 READY。

## 相关文档

-   前端：[Edge Web Console](./motrix_edge_web_console.md)
-   代码入口：`src/motrix_edge/__main__.py`（CLI）、`src/motrix_edge/node.py`（EdgeNode）—— 随 **feat/6**（node / CLI）与 **feat/3**（server）落地
-   计划：[开发计划](../plan/motrix_edge_development_plan.md)
