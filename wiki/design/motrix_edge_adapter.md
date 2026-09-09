# 机器人适配器（adapter / RobotAdapter）

## 摘要

`adapter/` 是**机器人硬件抽象层（HAL）**：核心（session / server / CLI）只依赖 `RobotAdapter`
接口与 **entry point 发现**；具体机器人由外部 SDK / 包实现本接口并注册。适配器是**薄客户端**：
身份与连接参数来自机器人进程 discover（`name` / `type` / `endpoint` / `shm_name`），能力
（动作维度 / 相机 / capabilities）由 adapter **类级常量**定义——连接参数类常量退化为缺省值，
指令经 HTTP 转发进程、观测经共享内存读取（SDK 自维护硬件与连接）。

## 目标与原则

1. **契约优先**：`RobotAdapter` 以抽象方法定义 HAL 契约，子类实现具体硬件逻辑。
2. **Entry point 发现**：`motrix_edge.adapters` group 是唯一注册机制；核心懒加载，`load()` 才 import。
3. **身份与连接来自进程、能力来自类**：discover 返回身份与进程自报连接参数（`endpoint` /
   `shm_name`），`action_dim` / 相机布局由类常量定义；连接参数类常量退化为缺省值（无 discover
   时兜底），保证「discover 可达 = 指令可靠可下达」。
4. **能力模型**：`AdapterCapability`（CAPTURE / EXECUTE / STREAMING）声明角色，会话按能力选择适配器。
5. **发现 + 实例化一步完成**：`discover_adapter(host, port)` 一步完成「发现 + 实例化」。

## 包结构

```
adapter/
├── __init__.py        # discover_adapter / get_adapter / robot_adapters / adapter_details（工厂 + entry point 发现）
├── base.py            # RobotAdapter ABC + AdapterCapability / RobotCapabilities / HealthStatus / CaptureStatus / DiscoveredRobot + 观测键契约
├── http_contract.py   # adapter ↔ SDK 进程的 HTTP 指令契约（端点 + body 字段单点定义）
├── shm_contract.py    # adapter ↔ SDK 进程的共享内存观测契约（ObsShmReader / ObsShmWriter）
├── http_shm_adapter.py    # HttpShmAdapter：HTTP 指令下行 + 共享内存观测上行的公共基类（行为全部在此）
├── test_adapter.py    # TestRobotAdapter（测试 / 无硬件联调；HttpShmAdapter 瘦子类）
└── dual_piper_adapter.py  # DualPiperAdapter（双臂 Piper；HttpShmAdapter 瘦子类）
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
| capture status  | `capture_status()`                  | 采集状态：运行位（是否正在采集）+ 元信息 + 数据目录 / 列表（默认 None）      |
| capture sync    | `sync_capture_meta(meta)`           | 把采集元信息同步到进程（保存一轮数据时附加）；默认 no-op                     |
| capture episode | `start_capture()` / `end_capture()` | 通知进程开始 / 结束一轮采集（episode）；默认 no-op                           |
| rollout         | `rollout(action)`                   | 推理闭环：接收模型 action，经 HTTP 转发进程限速靠近                          |
| safe_stop       | `safe_stop()`                       | 安全停止（幂等、失败安全）；**软停：停发指令 + 保持位姿，不断电**            |
| 生命周期辅助    | `reset()`                           | 程序复位到 home（非阻塞）                                                    |

### 观测键契约（standard_obs 键名）

单点定义于 `base.py`：`KEY_QPOS = "observations/qpos"`、`KEY_ACTION = "action"`、
`CAMERA_PREFIX = "observations/images/"`（相机名 `observations/images/<name>`）。

其中 `action` 是**进程侧当前目标动作**（SDK 侧正在执行的指令；尚无指令时进程回退为 qpos），
经共享内存单独传输，**不是 qpos 的副本**——所以 preview 显示的是真实指令。

设计取舍：

-   **observe 只读缓存、不采集**：观测由适配器自身持续运行更新；`observe()` 只取出缓存供
    「预览 + policy 推理」消费。数据采集（录制写盘）由适配器 / 进程自维护，**不驱动回合**，
    adapter 只预留 `capture_status()` 上报（运行位 + 元信息 + 数据目录 / 列表）。
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

## Adapter 驱动（discover 参数化）

Edge 配置**不含** adapter 身份，只配置「在哪里找」（`adapter` 段 host/port，缺省
`127.0.0.1:8090`）。Adapter 内部仍通过 `/v1/discover` 完成机器人进程探活与身份 / 连接参数获取：

```yaml
adapter:
    host: 127.0.0.1
    port: 8090
```

### Discover 契约（HTTP）

`POST /v1/discover` 响应 `robot` 块含**身份 + 进程自报的连接参数**（能力仍由 adapter 类常量定义）：

```json
{
    "status": "accepted",
    "robot": {
        "name": "test_robot_my_pc",
        "type": "test_robot",
        "running": true,
        "endpoint": "http://127.0.0.1:8090",
        "shm_name": "test_robot_obs"
    }
}
```

-   `name` / `type`：adapter 身份；`name` 供展示，`type` = adapter 类 entry point 名（用于加载并实例化）。
-   `endpoint` / `shm_name`：进程自报的连接参数（HTTP 指令地址 / 观测共享内存名），实例化时
    传入 adapter，**类常量 `SDK_URL` / `SHM_NAME` 退化为缺省值**。`endpoint` 取进程收到的
    请求 `Host`（可连地址），因此「discover 可达」即「指令可达」——换端口不必再同步改类常量。
-   `running`：进程是否运行（False = 未就绪，Edge 不绑定）。
-   `DiscoveredRobot` dataclass（`base.py`）：`name` / `type` + 可选 `endpoint` / `shm_name`。

### 工厂（adapter/**init**.py）

| 函数                                                     | 职责                                                                                                                                       |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `discover_adapter(host, port, required_capability=None)` | 发 `POST /v1/discover` 找进程；找到则**一步完成实例化**并返回 `RobotAdapter`；不可达 / 未运行 / 实例化失败 → `None`（节点持续重试）        |
| `get_adapter(discovered, required_capability=None)`      | **只做实例化**：按 `discovered.type` 经 entry point `load()` 实例化 `cls(name=..., endpoint=..., shm_name=...)`；类型未注册 → `ValueError` |
| `robot_adapters(required_capability=None)`               | 列出已注册适配器 `[(type, class, module)]`；缺省不触发类加载                                                                               |
| `adapter_details()`                                      | **静态列出全部**注册适配器 `[{type, available, capabilities}]`（不 discover / 不探活；缺 SDK 跳过）；暴露为 `GET /v1/adapters`             |

## 进程契约（adapter ↔ SDK）

-   **HTTP 指令下行**（`http_contract.py`）：端点路径 + body 字段**单点定义**。端点一览（前缀 `/v1`）：

| 方法 | 路径                                    | 请求 body         | 响应 body                                                                           |
| ---- | --------------------------------------- | ----------------- | ----------------------------------------------------------------------------------- |
| POST | `/v1/discover`                          | —                 | `{status, robot}`（身份 + 连接参数 `endpoint` / `shm_name` + `supported_adapters`） |
| GET  | `/v1/health`                            | —                 | `{ok, detail}`                                                                      |
| POST | `/v1/reset`                             | —                 | `{status}`                                                                          |
| POST | `/v1/execute`                           | `{action}`        | `{status}`                                                                          |
| POST | `/v1/rollout`                           | `{action: [dim]}` | `{status}`                                                                          |
| POST | `/v1/teleop`                            | `{enabled}`       | `{status}`                                                                          |
| POST | `/v1/safe_stop`                         | —                 | `{status}`                                                                          |
| GET  | `/v1/capture/status`                    | —                 | `{running, operator, task_name, meta, data_dir, data_files}`                        |
| POST | `/v1/capture/sync`                      | `{meta}`          | `{status}`                                                                          |
| POST | `/v1/capture/start` / `/v1/capture/end` | —                 | `{status}`                                                                          |

-   **共享内存观测上行**（`shm_contract.py`）：SDK 进程按 `run_hz` 持续把观测（qpos + 目标
    action + raw RGB 图像）写入共享内存（`ObsShmWriter`），adapter 经 `ObsShmReader` 读取并
    编码 JPEG 返回；布局版本随字段变化递增（当前 v2 = qpos + action + images，v1 仅 qpos +
    images，版本不一致时 attach 直接报错）。
    `read()` 对**无帧 / 撕裂帧 / 陈旧帧**一律返回 `None`：写者每帧更新 header `timestamp`，
    超过 `STALE_AFTER`（1s）未更新即判定陈旧（写者停机、或进程重启后旧段被同名重建）——
    此时不返回冻结帧，并按同一节奏尝试重新 attach 同名新段，避免把过期观测当实时观测。
-   **`/v1/safe_stop` 是软停**：停发指令 + 保持当前位姿（关节仍带力矩），**不断电**，机械臂不
    会失力下垂。**断电急停（硬件 e-stop）不属本契约**——由现场急停按钮 / 作业流程负责；待实现
    「掉力后受控阻尼下坠」流程后再评估接入（届时本契约与 robot-pipeline 侧需同步更新）。

## 内置适配器

内置适配器都是 `HttpShmAdapter` 的**瘦子类**：行为全部继承自基类，子类只声明类常量。

-   **HttpShmAdapter**（`http_shm_adapter.py`）：中间件型 adapter 的公共基类——指令经 HTTP
    下发到机器人进程、观测经共享内存读取（硬件与连接由进程自维护），实现 `RobotAdapter`
    的全部契约方法。子类只声明形态常量：`ADAPTER_TYPE` / `ACTION_DIM` / `IMAGES`
    （相机名 → 分辨率）/ `CAPABILITIES`，以及连接参数缺省值 `SDK_URL` / `SHM_NAME`。
-   **TestRobotAdapter**（`test_adapter.py`）：测试 / 无硬件联调；`ACTION_DIM` = 14，`IMAGES`
    = cam_head / cam_left_wrist / cam_right_wrist（640×480），共享内存 `test_robot_obs`。
-   **DualPiperAdapter**（`dual_piper_adapter.py`）：双臂 Piper（左右各 6 关节 + 夹爪 = 14）；
    相机布局与能力同 TestRobotAdapter，共享内存 `dual_piper_obs`；真实承载端是同仓的
    [robot-pipeline](../../robot-pipeline/README.md)。

## 接入方式（外部 SDK / 包）

1. 实现 `RobotAdapter` 子类（构造函数接收展示名称 `name`，`type` 由类常量 `ADAPTER_TYPE` 确定；
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
