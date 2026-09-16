# Robot Pipeline

基于 Python 的机器人数据采集与执行工具（Motphys）。

通过「机器人进程服务器（robot server）」对外提供统一的 `/v1` HTTP 指令契约与共享内存
观测，作为 **motrix-edge** 仓库中 Edge 侧 `adapter`（硬件抽象层）的真实承载端，实现：

-   **数据采集**：把每轮采集（episode）写为 Foxglove MCAP（ROS2 官方消息格式），并输出
    同名 JSON 元信息；
-   **控制**：`reset` / `execute` / `rollout` / `teleop` / `safe_stop` 指令；
-   **遥操作**：Leader→Follower / master→slave 主从跟随；
-   **策略部署 / 数据回放 / 可视化**：规划中（脚本尚未提供）。

本目录是 motrix-edge 仓库根下的独立子项目（自带 `pyproject.toml` / `uv.lock` /
`.venv`）；与 motrix_edge 的耦合**仅限契约**（HTTP 端点 / 观测键 / 共享内存布局单点定义
在 `motrix_edge.adapter`，robot server 直接复用），业务与硬件逻辑彼此不依赖。

## 目录结构

```text
scripts/
  start.sh                # 通用启动入口（交互菜单选配置 / 直接指定配置名）
  can_muti_activate.sh    # USB 物理口 → CAN 名绑定（用法见「CAN 总线配置」）
src/                      # robot-pipeline 包（src 布局，包名 config/env/robot/server/...）
  config/                 # robot server 配置 + 加载逻辑
    __init__.py           # load_config / get_config_dir（MOTRIX_CONFIG_DIR 优先，包内默认兜底）
    *.yml                 # test_robot / dual_piper / dual_alicia_piper / single_piper
  env/                    # BaseEnv：控制线程（30Hz 限速步进）/ 观测线程（取帧发布）/ 命令队列 / 采集控制
  robot/                  # BaseRobot + 具体机器人 + controller / sensor
  server/                 # robot_server.py 入口 + contract_server.py（/v1 契约服务器）
  collector/              # 数据采集（act_mcap：MCAP 流式写入 + 元信息 JSON）
  utils/                  # 工具（data_handler / load_file）
pyproject.toml / uv.lock
```

**不含 adapter/**：/v1 HTTP 端点、观测键、共享内存布局等契约**单点定义**在 motrix_edge
（`src/motrix_edge/adapter/` 的 `base.py` / `http_contract.py` / `shm_contract.py`），
robot server 经 `import motrix_edge.adapter.*` 复用，不本地拷贝。

## 架构分层

```text
Edge adapter（HTTP 指令下行 + 共享内存观测上行）   ← motrix_edge 侧薄客户端
        │  /v1/* HTTP
        ▼
robot server（contract_server：HTTP + 共享内存发布）
        │
        ▼
env（BaseEnv：控制线程 / 观测线程 + 命令队列 + 采集控制）
        │  控制拍 step() + sample_qpos()；观测拍 build_observation()
        ▼
robot（BaseRobot：action / target_action 限速逼近）
        │  get_observation_qpos()（控制线程）/ get_observation_images()（观测线程）
        ▼
collector（act_mcap：流式写 {uuid}.mcap + 元信息 JSON）
```

**控制 / 观测分线程**：`BaseEnv` 起**控制线程**（`HZ`=30Hz：运动指令 → `step()` 限速步进 →
`sample_qpos()` 采样机械臂状态）与**观测线程**（`OBS_HZ`=10Hz，**真实取观测的频率**：取相机帧 + 拼装观测 →
共享内存发布 → 采集落盘）；相机或磁盘卡顿只表现为观测丢帧，机械臂仍按 30Hz 稳定步进。
**队列 / 单写者 / 频率与诊断 / 停机语义等设计细节**见
[robot-pipeline 运行时](../wiki/design/robot_pipeline_runtime.md)（单一事实来源）。

一个 robot server 对应一个 Edge adapter：观测键（`observations/qpos`、`action`、
`observations/images/<cam>`）、HTTP 端点、共享内存布局都由 **motrix_edge.adapter** 下的
契约文件单点定义；robot server 复用这些定义，env 只负责控制 robot，不碰 HTTP / 共享内存。

## 依赖与运行前提

-   依赖 Python 3.10，使用 [uv](https://docs.astral.sh/uv/) 管理：

    ```bash
    uv sync
    ```

-   robot server import `motrix_edge.adapter.*` 契约 → 本子项目已在 `pyproject.toml` 把同仓
    `motrix-edge` 声明为 **path 依赖（editable）**，`uv sync` 会自动安装，无需手工配置
    `PYTHONPATH`；独立部署（不与本仓同目录）时，把 `[tool.uv.sources]` 中 `motrix-edge`
    的 `path` 指向自己的 motrix-edge 即可。
-   硬件可选依赖（仅在对应机器人上安装）：

    ```bash
    uv sync --extra realsense   # RealSense 相机（pyrealsense2）
    uv sync --extra v4l2        # V4L2 相机（v4l2-python3）
    uv sync --extra udev        # pyudev
    ```

## 机器人注册与配置

具体机器人实现（`BaseRobot` 子类）在 `src/robot/__init__.py` 的 `ROBOT_REGISTRY` 中注册
（类型名 -> 模块 + 类名）；配置 `robot.type` 即按此自动匹配：

| 注册类型                  | 实现                                                 | 说明                                 |
| ------------------------- | ---------------------------------------------------- | ------------------------------------ |
| `test_robot`              | `robot.test_robot.TestRobot`                         | 虚拟（无硬件，离线联调）             |
| `dual_piper_robot`        | `robot.dual_piper_robot.DualPiperRobot`              | 真实双臂接入位                       |
| `dual_alicia_piper_robot` | `robot.dual_alicia_piper_robot.DualAliciaPiperRobot` | 双臂：Alicia 主手 + Piper 从手遥操作 |
| `single_piper_robot`      | `robot.single_piper_robot.SinglePiperRobot`          | 单臂 Leader+Follower 遥操作          |

每个 robot server 一份配置（`src/config/*.yml`，作为 package data 随包提供），顶层键为
`INFO_LEVEL`（日志级别 DEBUG / INFO / ERROR，robot server 启动时读）、`server`（监听
host/port）、`robot`（name / type / step_rad / init_qpos / `ports` / `cameras`）、`collector`：

-   `test_robot.yml` —— `robot.type: test_robot`（默认，虚拟机器人无硬件）
-   `dual_piper.yml` —— `robot.type: dual_piper_robot`
-   `dual_alicia_piper.yml` —— `robot.type: dual_alicia_piper_robot`
-   `single_piper.yml` —— `robot.type: single_piper_robot`

配置加载分层（`src/config/__init__.py`，与 motrix_edge 同机制）：环境变量
`MOTRIX_CONFIG_DIR` 指向的外界配置目录优先，否则读包内默认 `*.yml`（只读兜底）。

`robot.name` 是**进程展示名**（可选，覆盖机器人类常量 `NAME`），也是 `/v1/discover` 上报给 Edge
的名字（控制台 / 状态接口显示的就是它）；不写则用机型默认名 `NAME`。同型号多台机器按机器命名
（如 `dual_piper_pc16`），便于控制台区分与采集元信息（mcap 同名 JSON 里的 `robot_name`）。

## 接入机器人

1.  在 `src/robot/controller/` 接入机械臂控制器，在 `src/robot/sensor/` 接入传感器；
2.  在 `src/robot/` 组装机器人（`BaseRobot` 子类），用**类常量**固定 obs/action 形态：
    -   `QPOS`：扁平动作维度（各臂关节 + 夹爪拼接）
    -   `IMAGE_NAMES` / `IMAGES`：相机名与分辨率
    -   `SHM_NAME`：观测共享内存名
    -   `CAPABILITIES`：能力声明（capture / execute / streaming）
    -   身份常量：`NAME`（展示名缺省值，配置 `robot.name` 可覆盖）/ `ADAPTER_TYPE` /
        `ROBOT_MODEL_ID` / `ROBOT_MODEL_VERSION`
3.  在 `src/robot/__init__.py` 的 `ROBOT_REGISTRY` 中注册；在 `src/config/*.yml` 配置
    `robot.type` 即按此自动匹配。

机器人控制配置见配置文件的 `robot` 段（`name` / `type` / `step_rad` / `init_qpos`）。**硬件接线参数**
也在此段描述（现场接线不同时只改这里，无需改代码）：

```yaml
robot:
    ports: # 控制器端口：CAN 接口名 / 串口设备节点（键 = 实现里的 PORT_ROLES）
        left_master: m_left # 左主手（遥操作输入）
        left: left # 左从臂（执行）
    cameras: # 相机设备（键 = 实现里的 IMAGE_NAMES）：RealSense 序列号 / V4L2 设备节点
        cam_head: "" # 头部序列号：现场填写（留空 → 启动即报错，不虚构占位值）
        cam_left_wrist: "" # 左腕序列号：现场填写（留空 → 启动即报错，不虚构占位值）
```

两个子段都是**键清单由代码声明、值必填**：缺失 / 未写 / 空串都会在机器人构造时报错——代码与
示例里都不放**现场值**，也不放**占位值**（假序列号只会把错误推迟到 SDK「找不到设备」）；未知
键名 → `WARNING`（帮助发现拼写错误）。
键名分别是实现里的 `PORT_ROLES` 与 `IMAGE_NAMES`（见 `src/robot/dual_piper_robot.py`、
`dual_alicia_piper_robot.py` 与 `single_piper_robot.py`）。相机值随机型不同：**RealSense 机型
（`dual_piper_robot`）三路都是 RealSense 序列号**；Alicia 机型（`dual_alicia_piper_robot`）
的腕相机是 V4L2 设备节点。

## CAN 总线配置

Piper 机械臂经 CAN 控制。`scripts/can_muti_activate.sh` 把**机械臂插的 USB 物理口**绑定到
**固定 CAN 名**（left / right / m_left / m_right）——换设备、重启、换顺序都不用改配置。

映射表维护在脚本顶部的 `USB_PORTS`：**键** = USB 物理口 `bus-info`（`ethtool -i <canX> |
grep bus-info` 的值，如 `3-2.2:1.0`；同一物理口稳定、与设备无关），**值** =
`<目标名>:<波特率>`。换 USB 口 / 换 HUB 时 `bus-info` 会变，用 `--list` 抄一遍即可。

```bash
sudo modprobe gs_usb                             # 前提：驱动已加载（无 CAN 接口时脚本会提示）
bash scripts/can_muti_activate.sh --list         # ① 看现状 ↔ 配置对照表（免 root），抄 bus-info
sudo bash scripts/can_muti_activate.sh --dry-run # ② 预演：只打印将要执行的 ip 命令
sudo bash scripts/can_muti_activate.sh           # ③ 绑定：down → 设波特率 → 改名 → up
```

-   参数：`--list`（只读对照表，免 root）/ `--dry-run`（只打印，免 root）/ `--ignore`（跳过
    「CAN 接口数 == 配置条数」的交互确认，非交互环境用）/ `-h`（用法）；
-   执行时会**短暂 down 接口并改名**：先确认机械臂已停止、没有正在跑的 CAN 通信；
-   退出码：`0` = 全部目标核对通过（接口存在 + link up + 波特率一致）；`1` = 有目标未达成或
    有 `ip` 命令失败；`2` = 参数错误。收尾会打印「目标态 vs 实际态」核对表，**以它为准**。

## 启动机器人服务

单一入口 `src/server/robot_server.py`，**按配置自动匹配**机器人（无需为每种机器人写
env/server）。配置名即 `src/config/` 下的文件名（`.yml` 后缀可省，默认 `test_robot.yml`）。

```bash
# 方式一：启动脚本（交互菜单选配置，dual_piper 排首位；直接指定配置名则跳过菜单）
bash scripts/start.sh
bash scripts/start.sh dual_piper.yml --host 0.0.0.0 --port 8090

# 方式二：直接运行（--config / --host / --port 均可覆盖）
uv run python src/server/robot_server.py --config test_robot.yml --host 0.0.0.0 --port 8090

# 方式三：uvicorn（app 默认按 $ROBOT_SERVER_CFG 或 test_robot.yml 构建）
ROBOT_SERVER_CFG=dual_piper.yml uv run uvicorn server.robot_server:app --host 0.0.0.0 --port 8090
```

监听地址默认取配置 `server.host` / `server.port`，命令行参数可覆盖。

## HTTP 指令契约（/v1）

robot server 提供以下端点（前缀 `/v1`，字段/端点单点定义见
`motrix_edge.adapter.http_contract`）：

| 方法 | 路径                 | 请求 body         | 说明                                                                            |
| ---- | -------------------- | ----------------- | ------------------------------------------------------------------------------- |
| POST | `/v1/discover`       | —                 | 自描述探活：身份 + 连接参数（`endpoint` / `shm_name`），Edge adapter 据此实例化 |
| GET  | `/v1/health`         | —                 | 健康检查 `{ok, detail, control_hz, measured_hz}`                                |
| POST | `/v1/reset`          | —                 | 复位到 home（非阻塞）                                                           |
| POST | `/v1/execute`        | `{action: [...]}` | 直接下发 raw 动作                                                               |
| POST | `/v1/rollout`        | `{action: [...]}` | 推理动作                                                                        |
| POST | `/v1/teleop`         | `{enabled: bool}` | 遥操作开关                                                                      |
| POST | `/v1/safe_stop`      | —                 | 安全停止（软停：停发指令 + 保持位姿，不断电）                                   |
| POST | `/v1/capture/start`  | —                 | 开始一轮采集（episode 开始）                                                    |
| POST | `/v1/capture/end`    | —                 | 结束一轮采集（episode 结束）                                                    |
| POST | `/v1/capture/sync`   | `{meta: {...}}`   | 同步采集元信息（operator / task_name 等）                                       |
| GET  | `/v1/capture/status` | —                 | 采集状态（运行位 / 元信息 / 数据目录）                                          |

另有调试端点 `GET /observe`（最新观测 qpos + 相机 JPEG base64）。

## 数据采集

采集配置见配置文件的 `collector` 段：

```yaml
collector:
    type: act_mcap # 采集器类型：act_mcap（当前唯一；act_hdf5 已移除）
    save_dir: ./data/test_robot # 采集数据保存目录
    image_format: jpeg # 图像编码：jpeg | raw
```

-   `act_mcap`：Foxglove MCAP（**ROS2 官方消息格式**，CDR 编码），每条 episode 保存为
    `{uuid}.mcap`（文件名用 UUID，全局唯一）；每信号一个 topic——`observations/qpos` /
    `action` 用 `std_msgs/msg/Float64MultiArray`，`observations/images/<cam>` 用
    `sensor_msgs/msg/CompressedImage`（JPEG）。帧时间取 obs 内 `timestamp` 作
    `log_time`，可在 Foxglove 中按时间轴查看；该 `timestamp` 是**观测拍时刻**（观测线程
    `build_observation()` 在取帧前打点），qpos / action 来自上一个控制拍——滞后**有界**（≤ 1/`HZ`
    ≈ 33ms）但**逐帧小幅波动**（两条独立限速循环的调度抖动；控制拍超时会让「最近一个控制拍」
    跳档），**不能按固定滞后做时间平移校正**。**流式写入**：`start` → `collect` 直接落盘 → `finish`。
-   采集由 HTTP 控制：`POST /v1/capture/start` 开始，`POST /v1/capture/end` 结束并落盘（在观测线程
    生效，≤ 1/`OBS_HZ`；与运动指令**跨队列顺序不保证**，见
    [运行时设计](../wiki/design/robot_pipeline_runtime.md)「跨队列顺序（有意弱化）」）。

### 采集元信息（meta）

collector 每轮采集维护一条元信息 `meta`，结束一轮后写为**与 mcap 同名**的 JSON
（`{uuid}.json`，即把 `.mcap` 后缀替换为 `.json`），用于描述该 mcap：

```json
{
    "relative_path": "383af95f31d744f5b49630125c5f0caf.mcap",
    "robot_name": "test_001",
    "robot_type": "test_robot",
    "operator": "Yu Hongzhen",
    "task_name": "put bowls",
    "frames": 370,
    "size_bytes": 12345678,
    "duration": 12.34,
    "sha256": "e3f0c1b8a2d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8g9h0i1j2k3l4m5n6o7p8q9r0",
    "created_at": "2026-09-01T10:00:00"
}
```

-   自动统计：`relative_path`（mcap 文件名）、`robot_name` / `robot_type`（env 注入）、
    `frames`（帧数）、`size_bytes`（文件字节）、`duration`（秒）、`sha256`（文件哈希）、
    `created_at`（采集开始时间，ISO）。
-   同步字段（`operator` / `task_name` / `description` 等）：默认留空（null），由 Edge
    侧 adapter 的 `capture sync` 同步——`POST /v1/capture/sync`，body `{"meta": {...}}`，
    在采集开始前或采集中同步均可；结束写 JSON 时附加。

## 遥操作

-   `single_piper`：Leader 主臂（读取）+ Follower 从臂（执行），同构映射；
-   `dual_piper` / `dual_alicia_piper`：master 主手（alicia）+ slave 从手（piper），
    左 → 左、右 → 右。

`POST /v1/teleop` `{"enabled": true}` 开启后，robot 的 `step()` 每帧从主臂读取目标并限速
跟随（`robot.step_rad`）；遥操作默认关闭（adapter 通讯控制中暂时均为 false）。`mode` 选映射模式：

-   `absolute`（缺省）：主臂**绝对**位姿直连从臂 target——主从同构、位姿已对齐的示教采集；
-   `delta`（**人工接管**）：`target = slave_ref + (master_now − master_ref)`——锚点在接管后
    首拍采样（主臂读数与从臂位姿同一拍），增量恒从 0 开始，从臂不会因主从位姿差突变；关节与
    夹爪同一套增量语义。模型即将失败时人工介入：先把主臂摆到与从臂相近的位姿，再
    `POST /v1/teleop {"enabled": true, "mode": "delta"}`。

两种模式的完整语义、锚点采样时机与边界见
[robot-pipeline 遥操作（绝对映射 / 增量接管）](../wiki/design/robot_pipeline_teleop.md)。

**遥操作期间推理让位**：遥操作开着（不分模式：遥操作即人工接管）时，`POST /v1/rollout` 返回
`409`（不改 target、不退出遥操作）；`execute` / `reset` / `safe_stop` 不受影响（执行即结束
遥操作）。Edge 侧 adapter 的 `rollout()` 据此返回 `False`，推理会话跳过该拍、遥操作关闭后
自动恢复。
