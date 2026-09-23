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

## 能力裁剪（启用臂 / 相机，RobotAdapter.configure）

机器人**只运行一个进程 / 一个 adapter**（不新增单臂 adapter）：`configure()` 是 **`RobotAdapter`
基类**的通用能力（`DualPiperAdapter` / `TestRobotAdapter` 声明臂布局后继承）。子类通过类常量声明
**动作布局**（`ARM_NAMES` / `ACTION_DIM_PER_ARM`（**按空间**每臂维度）/ `HOME`（**按空间**全臂 home）/
`DEFAULT_ENABLED_ARMS` / `IMAGES`），基类提供 `configure` / `_select_arm_segments` / `_expand_action`：

-   `enabled_arms`（right / left）：只启用部分臂时，各空间维度按**该空间的每臂维度 × 启用臂数**算
    （joint 6 / pose 6 / gripper 1），`execute` 按启用臂数接收动作，**未启用臂动作用同空间的
    `HOME[space]` 填充**（类常量，不参与运行时配置）；`observe()` 只返回启用臂的值（关节角 /
    关节段目标 / 夹爪 / 位姿各自裁剪，物理顺序 left → right 拼接）；
-   `enabled_cameras`：从 `IMAGES` 中挑选要暴露的相机（影响 `observe()` 与 `capabilities`）。
-   无臂概念（`ARM_NAMES` 为空）的 adapter 忽略 `enabled_arms`；参数**原子校验**：未知臂 / 未知
    相机 / 空臂 → `ValueError`，当前能力不变。不重启、不新建连接；缺省（全臂 / 全相机）与未裁剪
    行为一致。
-   **会话进行中（采集 / 推理）拒绝变更**：`configure()` 立即改变各空间维度与 `observe()`
    布局，episode 中途变化会让同一 episode 内 qpos / action 维度不一致（mcap 下游按固定维度
    解析），推理侧按旧维度下发的动作也会被维度校验拒绝——`POST /v1/adapters/config` 回 `409`、
    `adapter config set` 回 rejected；先 `session quit` 再改。
-   查询：`adapter config current` / `GET /v1/adapters/current` 回执 adapter **实际生效**的启用臂 /
    相机 / 各空间维度 / home；**未绑定 → rejected / 404**（能力布局是 adapter 类常量、discover
    不传，未绑定就没有机型信息——不猜、不回退某个 adapter 的默认）。

**运行时配置（不写 edge.yml）**：由命令 `adapter config set <json>` 或前端
`POST /v1/adapters/config`（受控操作，须租约）设置，存于节点运行时状态 `adapter_config`，在
adapter discover 绑定时应用（`_probe_adapter` → `apply_adapter_config`）；**未绑定 adapter 时
也先按类常量**（`ARM_NAMES` / `IMAGES`）**静态校验**（`normalize_capability_config()`，错误停在
set 时刻，而非等绑定失败、节点静默停在 IDLE）。非法配置 → 命令 rejected / HTTP 400，**会话进行
中 → rejected / HTTP 409**（先 `session quit`），两种情况下状态都不更新。查询用 `adapter config` /
`GET /v1/adapters/config`。缺省（全臂）行为与未裁剪一致：`action_dims = {joint: 12, pose: 12,
gripper: 2}`、动作直发。单臂任务下策略用通用 `act`（按启用臂数直通），「哪条臂 / 怎么映射回全臂
维度」全部由 adapter 承载。

## RobotAdapter 契约

职责面与「角色」一一对应：

| 职责面          | 方法                                 | 说明                                                                                                                                                |
| --------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| discover/health | `health()`                           | 健康检查；实时 `GET /v1/health`（SDK 型无后台心跳线程），缓存 `running`                                                                             |
|                 | `release()`                          | 释放本地资源（惰性 HTTP 客户端 / 共享内存读者）                                                                                                     |
| capabilities    | `capabilities`（属性）               | 声明能力：动作维度 / 支持的动作空间 / 观测键布局 / 能力 dict                                                                                        |
| observe         | `observe()`                          | 读取**最新观测缓存**（JPEG 图像 + qpos + 位姿，含 action）；**不推进 / 不影响运行**                                                                 |
| execute         | `execute(action)`                    | 直接下发 raw 动作（立即执行）                                                                                                                       |
| teleop          | `set_teleop(enabled, mode=None)`     | 遥操作 / **人工接管**（`mode=delta` 为锚点增量）；支持者记录 `teleop_enabled` / `teleop_mode` 供 server 状态上报，不支持者默认 no-op 且保持 `False` |
| capture status  | `capture_status()`                   | 采集状态：运行位（是否正在采集）+ 元信息（`meta`）+ 数据目录（默认 None）                                                                           |
| capture sync    | `sync_capture_meta(meta)`            | 把采集元信息同步到进程（保存一轮数据时附加）；默认 no-op                                                                                            |
| capture episode | `start_capture()` / `end_capture()`  | 通知进程开始 / 结束一轮采集（episode）；默认 no-op                                                                                                  |
| rollout         | `rollout(action, action_space=None)` | 推理闭环：接收模型 action 经 HTTP 转发进程（`action_space` 声明动作语义）；遥操作中进程拒收 → 返回 `False`                                          |
| safe_stop       | `safe_stop()`                        | 安全停止（幂等、失败安全）；**软停：停发指令 + 保持位姿，不断电**                                                                                   |
| 生命周期辅助    | `reset()`                            | 程序复位到 home（非阻塞）                                                                                                                           |

### 观测键契约（standard_obs 键名）

单点定义于 `base.py`：`KEY_QPOS = "observations/qpos"`（**关节角**）、`KEY_ACTION = "action"`
（关节段目标）、`KEY_GRIPPER = "observations/gripper"`（夹爪）、`KEY_POSE = "observations/pose"`
（**实测**末端位姿）、`KEY_POSE_TARGET = "observations/pose_target"`（**目标**位姿 =
`FK(关节段目标)`）、`CAMERA_PREFIX = "observations/images/"`（相机名
`observations/images/<name>`）。
`capabilities.observation_keys` 与 `observe()` 实际返回**同一套键**（qpos + action + gripper +
启用相机，机器人提供位姿时另有 pose / pose_target），随 `configure()` 实时变化；`image_names`
由相机键推理（`image_names_of()` 单点）。robot 进程 discover / health 上报的 `observation_keys`
同口径。

**观测常驻、与动作空间无关**：`qpos` 始终是关节角（数采 / VLA 要的就是它），`gripper` 是
独立的夹爪键（每臂 1），`pose` / `pose_target` 是**实测 / 目标**末端位姿（每臂 6 维 xyz + rpy，
米 / 弧度；机器人提供位姿时才有）——「目标 − 实测」就是底层 MIT 的实时稳态误差。
其中 `action` 是**进程侧当前目标动作**（SDK 侧正在执行的指令；尚无指令时进程回退为 qpos），
经共享内存单独传输，**不是 qpos 的副本**——所以 preview 显示的是真实指令。裁剪后
`qpos` / `action` / `gripper`（以及机器人提供位姿时的 `pose` / `pose_target`）共用同一个
`_select_arm_segments()` 口径，观测内臂维度始终自洽；位姿策略靠 `pose` 知道自己末端在哪（见
[边缘原语接口（primitives）](./motrix_edge_primitives.md)）。

**位姿约定（读 / 写必须同系）**：

-   **形状 / 单位**：每臂 `xyz(3) + rpy(3)` = 6 维、米 / 弧度（`pose_convention: xyz_rpy`）。
    SDK 若有 `0.001 mm` / `0.001°` 之类整数标度，由**机器人进程**换算后再写入位姿区。
-   **坐标系**：`POSE_FRAME`（如 `flange` / `tcp` / `fk`）声明"这是哪个系"。位姿观测与位姿
    动作**必须同一个系**——`move_delta` 这类「当前位姿 + 增量」差一个常量偏移就会打偏。
-   **能力以声明为准**：`effective_pose_dim_per_arm()` 取适配器声明的 `pose` 每臂维数（未声明
    `pose` 空间 → 0）；位姿键也只在声明时才进 `capabilities.observation_keys`，上游（agent / 策略）
    能提前拒绝而不是拿到空位姿再猜。读侧另有一道**实际长度**校验（见下条）。
-   **量纲防护**：位姿长度与（臂数 × 每臂维数）不符，或数值超出 `|xyz| ≤ 10 m` /
    `|rpy| ≤ 7 rad`（`http_shm_adapter.POSE_MAX_ABS_*`）→ 丢弃该拍 `pose` 并记一条 ERROR
    （同原因不刷屏）。宁可缺，也不把「把 0.001mm 当米」这类值喂进闭环。

### 动作空间（ActionSpace）

`ActionSpace`（`base.py`）声明 flat 动作向量的**语义**，三个空间**各自只表达一件事**、值都按臂
等长展开：`joint`（缺省：每臂 6 关节角，绝对目标）/ `pose`（每臂 xyz + rpy，绝对目标）/
`pose_delta`（每臂 xyz + rpy，**增量**，由机器人叠加在关节段目标上）/ `gripper`（每臂 1
夹爪，归一化 `[0, 1]`）。声明链路：调用方声明
（命令 `robot execute <value> [joint|pose|gripper]`；`adapter.rollout(action, action_space=...)`，
如原语接口的位姿原语）
→ 适配器校验（不在 `ACTION_SPACES` 内 / 维度不符 → `ValueError`）→ HTTP `/v1/execute` / `/v1/rollout`
的 `action_space` 字段 → 机器人进程解释（`pose` 走求解器；`gripper` 只写夹爪段）。缺省不发该字段时
机器人按关节空间解释，**向后兼容**；广告支持的空间与维度 = `GET /v1/adapters` 的
`capabilities.action_spaces` / `action_dims`。

⚠️ **机器人进程必须真的认 `action_space`**：若它忽略该字段并按关节空间解释，一份位姿
向量（与关节值同为每臂 6 维）会被当成关节角下发——**静默误解释**。因此机器人侧要：① `ActionRequest` 接受
`action_space`；② 三个空间分别「只写自己那一段」（`pose` 解算成关节后进同一条关节通路，夹爪另走一条）；
③ discover 里如实声明动作空间与各空间维度。

> 真机 dual piper 已接入：`pose` 的位姿在机器人侧由求解器解算成关节目标，再经
> `move_mit` 下发（**不经 `move_p`**，不引入第二套运动模式）——见
> [robot-pipeline 位姿动作](./robot_pipeline_cartesian.md)。解算失败（超限位 / 不收敛）
> 时机器人回 422 且不改目标，edge 侧因此不会拿到「被当关节角下发」的错误运动。

设计取舍：

-   **observe 只读缓存、不采集**：观测由适配器自身持续运行更新；`observe()` 只取出缓存供
    「预览 + policy 推理」消费。数据采集（录制写盘）由适配器 / 进程自维护，**不驱动回合**，
    adapter 只预留 `capture_status()` 上报（运行位 + 元信息 `meta` + 数据目录）。
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

| 方法 | 路径                                    | 请求 body                 | 响应 body                                                                           |
| ---- | --------------------------------------- | ------------------------- | ----------------------------------------------------------------------------------- |
| POST | `/v1/discover`                          | —                         | `{status, robot}`（身份 + 连接参数 `endpoint` / `shm_name` + `supported_adapters`） |
| GET  | `/v1/health`                            | —                         | `{ok, detail}`                                                                      |
| POST | `/v1/reset`                             | —                         | `{status}`                                                                          |
| POST | `/v1/execute`                           | `{action, action_space?}` | `{status}`                                                                          |
| POST | `/v1/rollout`                           | `{action, action_space?}` | `{status}`；**遥操作中 → 409**（推理让位）                                          |
| POST | `/v1/teleop`                            | `{enabled, mode?}`        | `{status}`（`mode`：`absolute` 缺省 / `delta` 人工接管）                            |
| POST | `/v1/safe_stop`                         | —                         | `{status}`                                                                          |
| GET  | `/v1/capture/status`                    | —                         | `{running, meta, data_dir}`                                                         |
| POST | `/v1/capture/sync`                      | `{meta}`                  | `{status}`                                                                          |
| POST | `/v1/capture/start` / `/v1/capture/end` | —                         | `{status}`                                                                          |

-   **共享内存观测上行**（`shm_contract.py`）：SDK 进程按 `run_hz` 持续把观测（qpos + 目标
    action + raw RGB 图像）写入共享内存（`ObsShmWriter`），adapter 经 `ObsShmReader` 读取并
    编码 JPEG 返回；布局版本随字段变化递增（当前 v2 = qpos + action + images，v1 仅 qpos +
    images，版本不一致时 attach 直接报错）。
    `read()` 对**无帧 / 撕裂帧 / 陈旧帧**一律返回 `None`：写者每帧更新 header `timestamp`，
    超过 `STALE_AFTER`（1s）未更新即判定陈旧（写者停机、或进程重启后旧段被同名重建）——
    此时不返回冻结帧，并按同一节奏尝试重新 attach 同名新段，避免把过期观测当实时观测。
    图像**路数**由 header `image_count` 声明，`observe()` 与 adapter 类常量 `IMAGES` 严格逐路
    配对：两侧相机布局声明不一致（进程少 / 多一路）时首次 `observe()` 即报错，而非静默截断。
-   **`/v1/safe_stop` 是软停**：停发指令 + 保持当前位姿（关节仍带力矩），**不断电**，机械臂不
    会失力下垂。**断电急停（硬件 e-stop）不属本契约**——由现场急停按钮 / 作业流程负责；待实现
    「掉力后受控阻尼下坠」流程后再评估接入（届时本契约与 robot-pipeline 侧需同步更新）。

## 内置适配器

内置适配器都是 `HttpShmAdapter` 的**瘦子类**：行为全部继承自基类，子类只声明类常量。

-   **HttpShmAdapter**（`http_shm_adapter.py`）：中间件型 adapter 的公共基类——指令经 HTTP
    下发到机器人进程、观测经共享内存读取（硬件与连接由进程自维护），实现 `RobotAdapter`
    的全部契约方法。子类只声明形态常量：`ADAPTER_TYPE` / `ACTION_DIM_PER_ARM`（按空间）/
    `HOME`（按空间）/ `ACTION_SPACES` / `IMAGES`（相机名 → 分辨率）/ `CAPABILITIES`，以及连接
    参数缺省值 `SDK_URL` / `SHM_NAME`。
-   **TestRobotAdapter**（`test_adapter.py`）：测试 / 无硬件联调；三空间维度
    `{joint: 12, pose: 12, gripper: 2}`，`IMAGES` = cam_head / cam_left_wrist / cam_right_wrist
    （640×480），共享内存 `test_robot_obs`。
-   **DualPiperAdapter**（`dual_piper_adapter.py`）：双臂 Piper（每臂 6 关节 + 1 夹爪；joint / pose
    每臂 6、gripper 每臂 1）；相机布局与能力同 TestRobotAdapter，共享内存 `dual_piper_obs`；
    真实承载端是同仓的 [robot-pipeline](../../robot-pipeline/README.md)。

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
