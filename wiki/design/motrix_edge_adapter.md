# 机器人适配器（adapter / RobotAdapter）

## 摘要

`adapter/` 是**机器人硬件抽象层（HAL）**：核心（session / server / CLI）只依赖 `RobotAdapter`
接口与 **entry point 发现**；具体机器人由外部 SDK / 包实现本接口并注册。适配器是**薄客户端**：
身份来自机器人进程 discover（`id` / `name` / `type`），能力与连接参数由 adapter **类级常量**定义，
指令经 HTTP 转发进程、观测经共享内存读取（SDK 自维护硬件与连接）。

## 目标与原则

1. **契约优先**：`RobotAdapter` 以抽象方法定义 HAL 契约，子类实现具体硬件逻辑。
2. **Entry point 发现**：`motrix_edge.adapters` group 是唯一注册机制；核心懒加载，`load()` 才 import。
3. **身份来自进程、能力来自类**：discover 只返回身份，`action_dim` / 相机布局 / 连接参数由类常量定义。
4. **能力模型**：`AdapterCapability`（CAPTURE / EXECUTE / STREAMING）声明角色，会话按能力选择适配器。
5. **发现 + 实例化一步完成**：`discover_adapter(host, port)` 一步完成「发现 + 实例化」。

## 包结构

```
adapter/
├── __init__.py        # discover_adapter / get_adapter / robot_adapters / adapter_details（工厂 + entry point 发现）
├── base.py            # RobotAdapter ABC + AdapterCapability / RobotCapabilities / HealthStatus / CaptureData / DiscoveredRobot + 观测键契约
├── http_contract.py   # adapter ↔ SDK 进程的 HTTP 指令契约（端点 + body 字段单点定义）
├── shm_contract.py    # adapter ↔ SDK 进程的共享内存观测契约（ObsShmReader / ObsShmWriter）
├── test_adapter.py    # TestRobotAdapter（测试 / 无硬件联调；HTTP + 共享内存薄客户端）
└── dual_piper_adapter.py  # DualPiperAdapter（双臂 Piper 骨架，预留接口未接实机）
```

## RobotAdapter 契约

职责面与「角色」一一对应：

| 职责面          | 方法                                | 说明                                                                         |
| --------------- | ----------------------------------- | ---------------------------------------------------------------------------- |
| discover/health | `health()`                          | 健康检查；实时 `GET /v1/health`（SDK 型无后台心跳线程），缓存 `running`      |
|                 | `release()`                         | 释放本地资源（惰性 HTTP 客户端 / 共享内存读者）                              |
| capabilities    | `capabilities`（属性）              | 声明能力：动作维度 / 观测键布局 / 能力 dict                                  |
| observe         | `observe()`                         | 读取**最新观测缓存**（JPEG 图像 + qpos，含 action）；**不推进 / 不影响运行** |
| execute         | `execute(action)`                   | 直接下发 raw 动作（立即执行）                                                |
| teleop          | `set_teleop(enabled)`               | 设置遥操作开关（true=遥操作 / false=程控）；默认 no-op                       |
| data_status     | `data_status()`                     | 采集数据状态：数据保存路径 + 数据列表（采集会话预留）                        |
| capture episode | `start_capture()` / `end_capture()` | 通知进程开始 / 结束一轮采集（episode）；默认 no-op                           |
| rollout         | `rollout(action)`                   | 推理闭环：接收模型 action，经 HTTP 转发进程限速靠近                          |
| safe_stop       | `safe_stop()`                       | 安全停止（幂等、失败安全）                                                   |
| 生命周期辅助    | `reset()`                           | 程序复位到 home（非阻塞）                                                    |

### 观测键契约（standard_obs 键名）

单点定义于 `base.py`：`KEY_QPOS = "observations/qpos"`、`KEY_ACTION = "action"`、
`CAMERA_PREFIX = "observations/images/"`（相机名 `observations/images/<name>`）。

设计取舍：

-   **observe 只读缓存、不采集**：观测由适配器自身持续运行更新；`observe()` 只取出缓存供
    「预览 + policy 推理」消费。数据采集（录制写盘）由适配器 / 进程自维护，**不驱动回合**，
    adapter 只预留 `data_status()` 上报。
-   **观测图像为 JPEG**（adapter 提供原图，如 640x480）；Edge 侧可解码 / 降采样后用于预览与 WebRTC。

### 能力模型（AdapterCapability）

```python
class AdapterCapability(str, Enum):
    CAPTURE = "capture"      # 支持数据采集（数据生产者）
    EXECUTE = "execute"      # 支持动作执行（推理闭环）
    STREAMING = "streaming"  # 支持视频流（遥操作预览 / 只读流）
```

-   类级 `CAPABILITIES`（dict）：供「不实例化」按能力列出 / 过滤（`robot_adapters(required_capability=...)`）。
-   实例 `capabilities.supports(cap)`：供会话实例化后校验（`CaptureSession` 要求 CAPTURE、
    `InferSession` 要求 EXECUTE，不支持 → `ValueError`）。
-   一个适配器可同时声明多种能力（如 `TestRobotAdapter`：CAPTURE + EXECUTE + STREAMING）。

## Discover 驱动（身份参数化）

Edge 配置**不含** adapter 身份，只配置「在哪里找」（`discover` 段 host/port，缺省
`127.0.0.1:8090`）：

```yaml
discover:
    host: 127.0.0.1
    port: 8090
```

### Discover 契约（HTTP）

`POST /v1/discover` 响应 `robot` 块**只含身份**（能力 / 连接参数由 adapter 类常量定义）：

```json
{
    "status": "accepted",
    "robot": { "id": "test_robot", "name": "Test Robot", "type": "test_robot", "running": true }
}
```

-   `id` / `name` / `type`：adapter 身份；`type` = adapter 类 entry point 名（用于加载并实例化）。
-   `running`：进程是否运行（False = 未就绪，Edge 不绑定）。
-   `DiscoveredRobot` dataclass（`base.py`）只保留 `id` / `name` / `type`。

### 工厂（adapter/**init**.py）

| 函数                                                     | 职责                                                                                                                                |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `discover_adapter(host, port, required_capability=None)` | 发 `POST /v1/discover` 找进程；找到则**一步完成实例化**并返回 `RobotAdapter`；不可达 / 未运行 / 实例化失败 → `None`（节点持续重试） |
| `get_adapter(discovered, required_capability=None)`      | **只做实例化**：按 `discovered.type` 经 entry point `load()` 实例化 `cls(name=..., id=...)`；类型未注册 → `ValueError`              |
| `robot_adapters(required_capability=None)`               | 列出已注册适配器 `[(type, class, module)]`；缺省不触发类加载                                                                        |
| `adapter_details()`                                      | **静态列出全部**注册适配器 `[{type, available, capabilities}]`（不 discover / 不探活；缺 SDK 跳过）；暴露为 `GET /v1/adapters`      |

## 进程契约（adapter ↔ SDK）

-   **HTTP 指令下行**（`http_contract.py`）：端点路径 + body 字段**单点定义**。端点一览（前缀 `/v1`）：

| 方法 | 路径                                    | 请求 body         | 响应 body                                              |
| ---- | --------------------------------------- | ----------------- | ------------------------------------------------------ |
| POST | `/v1/discover`                          | —                 | `{status, robot}`（自描述身份 + `supported_adapters`） |
| GET  | `/v1/health`                            | —                 | `{ok, detail}`                                         |
| POST | `/v1/reset`                             | —                 | `{status}`                                             |
| POST | `/v1/execute`                           | `{action}`        | `{status}`                                             |
| POST | `/v1/rollout`                           | `{action: [dim]}` | `{status}`                                             |
| POST | `/v1/teleop`                            | `{enabled}`       | `{status}`                                             |
| POST | `/v1/safe_stop`                         | —                 | `{status}`                                             |
| GET  | `/v1/data_status`                       | —                 | `{save_dir, data_files, running}`                      |
| POST | `/v1/capture/start` / `/v1/capture/end` | —                 | `{status}`                                             |

-   **共享内存观测上行**（`shm_contract.py`）：SDK 进程按 `run_hz` 持续把观测（raw RGB + 关节）
    写入共享内存（`ObsShmWriter`），adapter 经 `ObsShmReader` 读取并编码 JPEG 返回。

## 内置适配器

-   **TestRobotAdapter**（`test_adapter.py`）：测试 / 无硬件联调桩，HTTP + 共享内存薄客户端，
    能力 CAPTURE + EXECUTE + STREAMING；`IMAGES` = cam_head / cam_left_wrist / cam_right_wrist。
-   **DualPiperAdapter**（`dual_piper_adapter.py`）：双臂 Piper 骨架（预留接口，方法体
    `NotImplementedError` 占位，未接实机 SDK）；能力声明同 Test。

## 接入方式（外部 SDK / 包）

1. 实现 `RobotAdapter` 子类（构造函数接收身份 `name` / `id`，`type` 由类常量 `ADAPTER_TYPE` 确定；
   声明类级 `CAPABILITIES` 与连接 / 能力类常量）。
2. 在外部包 `pyproject.toml` 声明 entry point（名 = adapter 类型）：

    ```toml
    [project.entry-points."motrix_edge.adapters"]
    alicia_piper = "vendor_sdk.robot:AliciaPiperAdapter"
    ```

3. 安装外部包；discover 返回的 `type` 与 entry point 名一致时即可实例化。验证：
   `motrix-edge adapters list` 应列出该适配器。

## 相关文档

-   节点绑定 / 复用：[节点生命周期（node）](./motrix_edge_node.md)
-   会话按能力选择：[会话（session）](./motrix_edge_session.md)
-   观测语义与预览：[FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md)
-   代码入口：`src/motrix_edge/adapter/` —— 随 **feat/6**（任务运行时核心）落地
