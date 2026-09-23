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
  setup_robot.sh          # 机器档案一键生成：交互逐个插设备 → 相机序列号 / 串口 / CAN 物理口
  can_muti_activate.sh    # 按机器档案把 USB 物理口绑到固定 CAN 名（用法见「CAN 总线配置」）
src/                      # robot-pipeline 包（src 布局，包名 config/env/robot/server/...）
  config/                 # robot server 配置 + 加载逻辑 + 机器档案探测
    __init__.py           # load_config（<根>/config 播种副本 → 机器档案深合并 → 包内示例兜底）
    setup.py / probe.py   # 一键填档（交互逐个插）+ 设备枚举（can / serial / realsense / v4l2）
    *.yml                 # **示例**配置（test_robot / dual_piper / dual_alicia_piper / single_piper）——
                          # 实际配置在 <根>/config/：<机型>.yml（整份）或 robot/<machine>.yml（差异）
  env/                    # BaseEnv：控制线程（30Hz 限速步进）/ 观测线程（取帧发布）/ 命令队列 / 采集控制
  robot/                  # BaseRobot + 具体机器人 + controller / sensor
    kinematics/           # 运动学（纯 numpy Modified DH）：正解 / 雅可比 / 逆解
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

一个 robot server 对应一个 Edge adapter：观测键（`observations/qpos` **状态向量** = 每臂「值 +
夹爪」交错、`action` **目标向量** = 同维同布局、`observations/images/<cam>`，机器人提供末端位姿时
另有 `observations/pose` / `observations/pose_target`）、HTTP 端点、共享内存布局都由
**motrix_edge.adapter** 下的契约文件单点定义；robot server 复用这些定义，env 只负责控制 robot，
不碰 HTTP / 共享内存。

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
host/port）、`robot`（name / type / step_rad / init_joint / `ports` / `cameras`）、`collector`：

-   `test_robot.yml` —— `robot.type: test_robot`（默认，虚拟机器人无硬件）
-   `dual_piper.yml` —— `robot.type: dual_piper_robot`
-   `dual_alicia_piper.yml` —— `robot.type: dual_alicia_piper_robot`
-   `single_piper.yml` —— `robot.type: single_piper_robot`

配置加载（`src/config/__init__.py`，与 motrix_edge 同机制）：**包内 `src/config/*.yml` 只是示例**
（随包分发、只读、零现场值）。实际配置在**一个根目录**下——`<根> = $MOTRIX_ROBOT_PIPELINE_DIR`，
未设时回落 `<cwd>/motrix-robot-pipeline/`（配置与日志同一个根）：

1. `<根>/config/<机型>.yml`：整份机型配置（首次访问把包内示例**播种**过去，之后以它为准）；
2. `<根>/config/robot/<machine>.yml`：机器档案，只写差异，深合并到机型 yml 上（见下节）；
3. `<根>/config/gravity/*.json`：重力参数标定产物（`scripts/fit_gravity.py --install` 写入）。

日志与环境变量（与 motrix_edge **共用同一套变量名**，同一份仓库根 `.env`；实现各自独立）：

-   **日志级别**：只读该机型 yml 的 `INFO_LEVEL`（`DEBUG` / `INFO` / `WARNING` / `ERROR`），不写 →
    代码缺省 `INFO`；**没有环境变量开关**（多一个开关就会静默盖掉 yml）；启动时经
    `set_log_level()` 解析一次，**不写 `os.environ`**；
-   **文件日志开关**：`MOTRIX_EDGE_LOG_FILE`（缺省关闭）；开启后写 `<根>/logs/`
    （`<根> = $MOTRIX_ROBOT_PIPELINE_DIR`，未设 → `<cwd>/motrix-robot-pipeline/`），与 edge 的
    `$MOTRIX_EDGE_DIR/logs/` 分开；
-   **uvicorn 日志**：与 edge 同构（`utils/logging.uvicorn_log_config`）——HTTP access 缺省
    **静默**（防长期运行刷屏）；开启 `MOTRIX_EDGE_LOG_FILE` 后 access 只写 `<日志目录>/uvicorn.log`
    （轮转 10MB × 5，纯文本），uvicorn 启动 / 错误日志始终写终端。⚠️ 只有
    `python src/server/robot_server.py`（走 `serve()`）会应用该配置；用
    `uvicorn server.robot_server:app` 启动时是 uvicorn 默认行为；
-   **裸跑（无 docker）** 复用仓库根 `.env`：`uv run --env-file ../.env python src/server/robot_server.py`。

`robot.name` 是**进程展示名**（可选，覆盖机器人类常量 `NAME`），也是 `/v1/discover` 上报给 Edge
的名字（控制台 / 状态接口显示的就是它）；不写则用机型默认名 `NAME`。同型号多台机器按机器命名
（如 `dual_piper_pc16`），便于控制台区分与采集元信息（mcap 同名 JSON 里的 `robot_name`）。

## 接入机器人

**职责边界（依赖方向：robot → controller → kinematics）**：`robot/kinematics` 是纯运动学 / 求解器
（无状态）；`PiperController` 只管**关节 / 夹爪读写 + 限位把关**，并对外提供**静态**位姿转换
（`joint_to_pose` / `pose_to_joint`）；`BaseRobot` **只有骨架**（目标状态机、逐拍限速、遥操作映射、
观测组装）；机器人类**编排**——robot 收到 `pose` 目标时自己调解算得到关节角，再进同一条控制通路。

1.  在 `src/robot/controller/` 接入机械臂控制器（需提供 `get_joint` / `set_joint` / `get_gripper` /
    `set_gripper`；要支持 `pose` 动作还需**静态** `joint_to_pose`（正解）/ `pose_to_joint`（解算，
    只解算不下发）），在 `src/robot/sensor/` 接入传感器；
2.  在 `src/robot/` 组装机器人（`BaseRobot` 子类），用**类常量**固定 obs/action 形态：
    -   `QPOS` / `GRIPPER`：`joint` 空间维度（各臂关节角拼接）与 `gripper` 空间维度（每臂 1 夹爪）
    -   `ARM_NAMES` / `ARM_CONTROLLERS`：臂名（物理顺序）与「臂 → `controllers` 键」映射——自己的取数 /
        下发按它遍历
    -   `JOINTS_PER_ARM` / `POSE_DIM_PER_ARM`：每臂关节数（6）与每臂位姿维数（6）；每臂夹爪 1
    -   `POSE`：扁平末端位姿维度（各臂 `xyz + rpy`；**0 = 不提供位姿观测**）——由静态正解给出，
        robot server 据此把**实测位姿**与**目标位姿**写入共享内存（布局 v5 的 pose / pose_target 区）
    -   `GRIPPER_DEADZONE`：夹爪死区（piper 为 0.2；缺省 0）
    -   `IMAGE_NAMES` / `IMAGES`：相机名与分辨率
    -   `SHM_NAME`：观测共享内存名
    -   `ACTION_SPACES`：支持的动作空间（四个：`joint` / `pose` / `pose_delta` / `gripper`；缺省
        `joint` + `gripper`）——控制器能解算 `pose` 时才声明它（`pose_delta` 依赖
        `get_target_pose()`），Edge 侧 adapter 才能宣称
    -   `CAPABILITIES`：能力声明（capture / execute / streaming）
    -   身份常量：`NAME`（展示名缺省值，配置 `robot.name` 可覆盖）/ `ADAPTER_TYPE` /
        `ROBOT_MODEL_ID` / `ROBOT_MODEL_VERSION`
3.  在 `src/robot/__init__.py` 的 `ROBOT_REGISTRY` 中注册；在 `src/config/*.yml` 配置
    `robot.type` 即按此自动匹配。

机器人控制配置见配置文件的 `robot` 段（`name` / `type` / `step_rad` / `init_joint` / `cartesian`）。
**硬件接线参数**也在此段描述（现场接线不同时只改这里，无需改代码）：

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

这些值**每台机器不同** → 写进**实际配置目录** `<根>/config/`（包内 yml 只是零现场值的示例），有两种落点（见下节）。

## 机器档案（每台机器一份，现场值不进仓库）

每台机器不同的只有几处：CAN 接口名 / 串口节点、相机序列号或设备节点、机器名、数据目录、
（可选）各臂重力参数，以及 CAN 的 **USB 物理口 → 固定接口名**映射（`can.bindings`）。
它们放在**实际配置目录** `<根>/config/`（`<根> = $MOTRIX_ROBOT_PIPELINE_DIR` 或 `<cwd>/motrix-robot-pipeline`），
两种落点：

| 落点         | 路径                              | 语义                                                                |
| ------------ | --------------------------------- | ------------------------------------------------------------------- |
| **机型 yml** | `<根>/config/<机型>.yml`          | 这台机器的**整份**配置（robot server 直接读它；包内示例已播种过来） |
| **机器档案** | `<根>/config/robot/<machine>.yml` | 只写**差异**，叠在机型 yml 上（深合并）                             |

两种都能用（以机器档案为例，也可写成机型 yml）：

```yaml
machine: pc16
robot:
    name: dual_piper_pc16
    ports: { left_master: m_left, right_master: m_right, left: left, right: right }
    cameras: { cam_head: "12362227", cam_left_wrist: "...", cam_right_wrist: "..." }
    gravity:
        arms:
            left: { params: gravity/piper_pc16_left.json } # 每臂一份标定产物
collector: { save_dir: ./data/pc16 }
can:
    bindings: # USB 物理口(bus-info) -> <目标CAN名>:<波特率>（can_muti_activate.sh 读它）
        "3-2.2:1.0": left:1000000
        "3-2.1:1.0": right:1000000
        "3-1.3:1.0": left_master:1000000
        "3-1.5:1.0": right_master:1000000
```

-   **生效**：启动时按 `--machine <名>` → `MOTRIX_ROBOT_PIPELINE_MACHINE` → `hostname` 解析机器档案，与机型
    yml **深合并**（映射递归合并、列表整体替换）——档案只写差异，机型默认一改就跟着变；
    走 `--target config` 写整份机型 yml 时则**直接改实际配置**（代价：包内示例的后续更新不再自动生效）；
-   **没档案照常跑**：环境变量 / hostname 解析出的名字没有档案时静默跳过（等价于现状）；但
    `--machine` 显式指定而档案不存在 → **报错**（避免「以为加载了档案」）；
-   启动日志先打一段**启动清单**（`config` / `machine` / `robot` / `log` / `collect` 五行）：
    `config` 行给出实际读到的文件（本地副本 / 包内示例），`machine` 行给出实际用的档案
    （如 `pc16（档案 …/pc16.yml）` / `pc16（无档案 → 按机型默认）`），`robot` 行给出类型与接线
    摘要，`collect` 行给出**绝对**落盘目录（相对路径按启动时 cwd 解析）。

### 一键生成：`scripts/setup_robot.sh`

```bash
export MOTRIX_ROBOT_PIPELINE_DIR=$HOME/motrix-robot-pipeline # 可选：配置 / 日志根（未设 → <cwd>/motrix-robot-pipeline）
bash scripts/setup_robot.sh --machine pc16               # 交互：写机器档案（默认，只写差异）
bash scripts/setup_robot.sh --config dual_piper --machine pc16 --target config  # 写整份机型 yml
bash scripts/setup_robot.sh --config dual_piper --machine pc16 --list        # 只看现状（现场设备 + 现有配置）
bash scripts/setup_robot.sh --config dual_piper --machine pc16 --dry-run     # 只打印将写入的补丁
bash scripts/setup_robot.sh --config dual_piper --machine pc16 --can  # 写完顺带 sudo 激活 CAN
```

脚本按机器人类声明的 `PORT_KINDS` / `CAMERA_KINDS`（`can` / `serial` / `realsense` / `v4l2` /
`virtual`）逐个提示：**每次只插提示的那一个设备**，插好后回车——识别靠「插拔差分」，所以
**不必知道序列号 / bus-info**，也不会出现「左右腕颠倒」这类数据里极难发现的错。声明漏了
（`*_KINDS` 与 `PORT_ROLES` / `IMAGE_NAMES` 不一致）→ 直接报错要求补，**不猜**。

写进去的都是**稳定标识**：CAN 记 USB 物理口（`bus-info`，换臂不换口就不变）、串口记
`/dev/serial/by-id/*`、USB 相机记 `/dev/v4l/by-id/*`、RealSense 记序列号。

## CAN 总线配置

Piper 机械臂（含主手）经 CAN 控制。`scripts/can_muti_activate.sh` 把**USB 物理口**绑定到
**固定 CAN 名**（left / right / m_left / m_right）——换设备、重启、换顺序都不用改配置。

绑定表**不在脚本里硬编码**，而是 **robot 配置里的 `can.bindings`**（键 = USB 物理口 `bus-info`，
值 = `<目标名>:<波特率>`）：脚本通过 `config.load_config` 读它，**与 robot server 同一套加载**
（外界机型 yml 优先 + 机器档案叠加），所以两种落点都行。换 USB 口 / 换 HUB 时 `bus-info`
会变 → 重跑 `scripts/setup_robot.sh` 即可。读表时会顺带校对
`robot.ports` 的 CAN 角色值与 bindings 目标名是否一致（对不上会直接报错，免得到现场才发现连不上）。

```bash
sudo modprobe gs_usb                                    # 前提：驱动已加载（无 CAN 接口时脚本会提示）
bash scripts/can_muti_activate.sh --list                # ① 看现状 ↔ 配置对照表（免 root，自动读配置）
sudo bash scripts/can_muti_activate.sh --dry-run        # ② 预演：只打印将要执行的 ip 命令
sudo bash scripts/can_muti_activate.sh --config dual_piper   # ③ 绑定：down → 设波特率 → 改名 → up
```

-   参数：`--config <机型|yml 路径>`（缺省 `$MOTRIX_ROBOT_PIPELINE_CFG` → 否则机器档案
    `<根>/config/robot/${MOTRIX_ROBOT_PIPELINE_MACHINE:-$(hostname)}.yml`）/ `--machine <名>` /
    `--list`（只读对照表，免 root）/ `--dry-run`（只打印，免 root）/ `--ignore`（跳过「CAN 接口数
    == 配置条数」的交互确认，非交互环境用）/ `-h`（用法）；
-   **绑定不持久**：`ip link` 的改名 / 波特率 / up 重启后都不保留 → 每次开机要重跑一次；要免手动
    就把它接到开机脚本 / systemd unit（`ExecStart=... can_muti_activate.sh --config dual_piper`）；
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

# 方式三：uvicorn（app 默认按 $MOTRIX_ROBOT_PIPELINE_CFG 或 test_robot.yml 构建）
MOTRIX_ROBOT_PIPELINE_CFG=dual_piper.yml uv run uvicorn server.robot_server:app --host 0.0.0.0 --port 8090
```

监听地址默认取配置 `server.host` / `server.port`，命令行参数可覆盖。

## HTTP 指令契约（/v1）

robot server 提供以下端点（前缀 `/v1`，字段/端点单点定义见
`motrix_edge.adapter.http_contract`）：

| 方法 | 路径                 | 请求 body                         | 说明                                                                            |
| ---- | -------------------- | --------------------------------- | ------------------------------------------------------------------------------- |
| POST | `/v1/discover`       | —                                 | 自描述探活：身份 + 连接参数（`endpoint` / `shm_name`），Edge adapter 据此实例化 |
| GET  | `/v1/health`         | —                                 | 健康检查 `{ok, detail, control_hz, measured_hz}`                                |
| POST | `/v1/reset`          | —                                 | 复位到 home（非阻塞）                                                           |
| POST | `/v1/execute`        | `{action: [...], layout?, arms?}` | 直接下发 raw 动作（`layout="pose"` → 机器人侧解算）                             |
| POST | `/v1/rollout`        | `{action: [...], layout?, arms?}` | 推理动作（**遥操作中 → 409**：推理让位；IK 失败 → 422）                         |
| POST | `/v1/teleop`         | `{enabled, mode?}`                | 遥操作（`mode` 缺省 `absolute`；`delta` = 人工接管增量）                        |
| POST | `/v1/safe_stop`      | —                                 | 安全停止（软停：停发指令 + 保持位姿，不断电）                                   |
| POST | `/v1/capture/start`  | —                                 | 开始一轮采集（episode 开始）                                                    |
| POST | `/v1/capture/end`    | —                                 | 结束一轮采集（episode 结束）                                                    |
| POST | `/v1/capture/sync`   | `{meta: {...}}`                   | 同步采集元信息（operator / task_name 等）                                       |
| GET  | `/v1/capture/status` | —                                 | 采集状态（运行位 / 元信息 / 数据目录 / 帧头跳过进度）                           |

另有调试端点 `GET /observe`（最新观测 qpos + 末端位姿（提供时）+ 相机 JPEG base64）。

## 数据采集

采集配置见配置文件的 `collector` 段：

```yaml
collector:
    type: act_mcap # 采集器类型：act_mcap（当前唯一；act_hdf5 已移除）
    save_dir: ./data/test_robot # 采集数据保存目录
    image_format: jpeg # 图像编码：jpeg | raw
    # 帧头跳过（只对**遥操作录制**生效）：capture 开始后主臂相对首帧未超过阈值 → 不记录
    # （连 episode 都不开），直到出现一次有效移动；此后微小位移照常记录，下一轮重新武装
    skip_until_motion:
        enabled: true # false = 关闭（首帧即记录）
        joint_eps: 0.05 # 关节有效移动阈值（rad，取 max|Δq|）
        gripper_eps: 0.05 # 夹爪有效移动阈值（归一化 [0,1]，取 max|Δg|）
```

-   `act_mcap`：Foxglove MCAP（**ROS2 官方消息格式**，CDR 编码），每条 episode 保存为
    `{uuid}.mcap`（文件名用 UUID，全局唯一）；每信号一个 topic——`observations/qpos`（**状态向量**
    = 每臂「值 + 夹爪」交错）/ `action`（**目标向量**，同维同布局）用
    `std_msgs/msg/Float64MultiArray`，`observations/images/<cam>` 用
    `sensor_msgs/msg/CompressedImage`（JPEG）。**逐维含义写在同名 `{uuid}.json`**
    （`state_space` / `state_dims` / `action_space` / `action_dims`，每项 `{index, arm, kind, name}`）——
    机器人整体切位姿时只改 `state_space`，下游不必改代码。帧时间取 obs 内 `timestamp` 作
    `log_time`，可在 Foxglove 中按时间轴查看；该 `timestamp` 是**观测拍时刻**（观测线程
    `build_observation()` 在取帧前打点），qpos / action 来自上一个控制拍——滞后**有界**（≤ 1/`HZ`
    ≈ 33ms）但**逐帧小幅波动**（两条独立限速循环的调度抖动；控制拍超时会让「最近一个控制拍」
    跳档），**不能按固定滞后做时间平移校正**。**流式写入**：`start` → `collect` 直接落盘 → `finish`。
    **帧头跳过（只对遥操作录制）**：`robot teach` / `robot takeover` 开着时，capture 开始后主臂相对
    首帧未超过 `skip_until_motion` 阈值前不记录（也不创建 episode 文件），出现一次有效移动才开始；
    跳过只作用于本轮帧头，下一轮重新武装。进度见日志与 `GET /v1/capture/status` 的 `head_skip`。

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
    "control_mode": "mit+gravity",
    "control": {
        "left_arm": {
            "mode": "mit+gravity",
            "ctrl_mode": "mit",
            "role": "follower",
            "gravity": {
                "active": true,
                "alpha": 1.0,
                "limit_nm": 16.0,
                "params": "gravity/piper_6dof.json",
                "placeholder": false
            }
        }
    },
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
-   控制模式（env 注入）：`control_mode` = 各**执行**臂的聚合值（`mit` / `mit+gravity` /
    `joint`；双臂不同 → `mixed`；没有执行控制器 → `null`）+ `control` = 逐控制器明细
    （`mode` / `ctrl_mode` / `role` / `gravity`：`active` / `alpha` / `limit_nm` / `params` /
    `placeholder`）。**主手（leader）不记录**。注意 `joint` 通路不下发 `t_ff`，重力前馈在该
    模式下**不生效**（此时 `mode` 只报 `joint`）。
-   同步字段（`operator` / `task_name` / `description` 等）：默认留空（null），由 Edge
    侧 adapter 的 `capture sync` 同步——`POST /v1/capture/sync`，body `{"meta": {...}}`，
    在采集开始前或采集中同步均可；结束写 JSON 时附加。

## 位姿动作（末端位姿）

机器人只暴露**一条**控制通路：关节目标 + MIT（`move_mit`）。`layout="pose"` 是
**动作语义**，不是控制模式——机器人收到每臂 `xyz + rpy + 夹爪` 后，由**自己**用仓库内运动学
（`src/robot/kinematics/`，纯 numpy Modified DH）通过控制器的**静态**转换函数**解算一次**，
再按关节目标下发（限速、采样、观测全部不变），**不切 `move_p`**（SDK 的 `move_mit` / `move_j` /
`move_p` 互斥，交替下发会逐帧互切）。

运动学转换是**无状态静态函数**（`PiperController.joint_to_pose` / `pose_to_joint`），机器人类负责
编排（按臂拆包 → 解算 → 写目标）——新增带位姿动作的机器人只需提供同形静态函数，不碰控制循环。

-   `POST /v1/execute` / `POST /v1/rollout` 的 body 用 `layout` 声明动作语义
    （缺省 `joint`）：单段 `joint` / `pose` / `pose_delta` / `gripper` 各自只写自己那一段，
    组合段用 `+` 连接（如 `joint+gripper`），**各段在同一控制拍落地**。
-   **只控部分臂**时加 `arms`（如 `layout="joint+gripper"` + `arms=["right"]`）：`action` 按
    `arms` 顺序逐臂块拼接，每臂块 = 按 `layout` 段序（如 6 关节 + 1 夹爪）——**未选臂不补 home**
    （关节 / 夹爪目标原样保留）。给「模型只观察 / 只控制右臂」这类单臂 VLA 用；臂名 / 长度 /
    段名 / 有限性不符 → 422 且不改目标。
-   **末端位姿观测**（`observations/pose`）由**同一套运动学**正解给出：观测与解算同模型、
    同坐标系，所以「读到的位姿」与「下发的目标」可直接比对；SDK 的 `get_flange_pose()` 仅用于
    **标定对照**（`scripts/verify_cartesian.py`），**不是运行时依赖**（底层只需关节读写 + MIT）。
-   **解算失败**（超限位 / 不收敛）→ HTTP `422` 且**不改既有目标**（机械臂保持原动作）；
    `layout` 里的段未被机器人声明（或其字段名写错）时同样报错，不会把位姿向量当关节角静默下发。
-   **限位只有一份**：解算用的限位与 `set_joint` **下发前**的裁切共用 `PIPER_JOINT_LIMITS`
    （同一份，**不经配置覆盖**）——越界值一帧都不交给 SDK（否则 SDK 会报错并打印），裁到时打一条
    WARNING（同一组超限关节只打一条）。
-   **只下关节角**：`set_joint` 只给 MIT 的 `p_des`，`kp` / `kd` 用缺省值；`t_ff` 缺省为 0，
    也可由**重力前馈**给出（见下节「重力补偿」）——**不做力控**，也不补科氏 / 摩擦。

配置（`robot` 段，全部可选）：

```yaml
robot:
    cartesian:
        ik: { pos_tol: 0.0001, rot_tol: 0.001, max_iters: 200, damping: 0.01, step_limit: 0.2 }
```

**现场标定**：DH 参数 / 关节零位必须与真机一致，用 `scripts/verify_cartesian.py` 对照 `fk(q)`
与 SDK 法兰位姿（默认**只读**，`--cycles` 才小幅摆动）：

```bash
python scripts/verify_cartesian.py --port left           # 只读对照 + IK 往返（推荐先跑）
python scripts/verify_cartesian.py --port left --cycles 3 --role follower
```

## 重力补偿（可选，默认占位 = 行为不变）

MIT 的 `t_ff` 缺省是 0（不补重力），所以关节会停在 `τ_g / k_p` 的平衡点：低刚度或带负载时
「设定什么角度就是什么角度」并不成立。重载 / 想做柔顺时，可离线标定出 `τ̂_g(q)` 并前馈：

1. **采样**（真机，`--sweep` 会运动）：`python scripts/verify_gravity.py --port can_left --sweep --out gravity_samples.json`
   ——先 `--read` 确认读数可用（含单拍耗时）、`--hold` 确认 **τ_meas 与下发的符号 / 单位一致**；
2. **拟合**（任意机器，离线）：`python scripts/fit_gravity.py --samples gravity_samples.json --install`
   ——写进控制器读的参数文件（`config/gravity/*.json`）；报告里的**秩 / 条件数 / 残差 RMS** 决定这次标定能不能用；
3. **验收**：`python scripts/verify_gravity.py --port can_left --hold --alpha 1.0` 与 `--alpha 0`
   在**同一姿态**下对比 `k_p·|Δq|`（首跑建议 0.2 → 0.5 → 1.0 逐档）。

运行时每拍 `t_ff = clip(α · τ̂_g(实测 q), ±t_ff_limit)`；**任何异常（读数读不到 / NaN / 超限幅）
当拍退回 `t_ff = 0`**（退回纯位置环）并告警一条，控制不中断。参数按**臂**归档（双臂不能共用），
**换负载（工件 / 夹爪）必须重标**。设计与取舍见
[`wiki/design/robot_pipeline_impedance.md`](../wiki/design/robot_pipeline_impedance.md)。

```yaml
robot:
    gravity:
        enabled: true # 缺省 true + 占位参数（全 0）→ t_ff 恒为 0，行为与未补偿完全一致
        alpha: 1.0 # 前馈比例（0 = 不补偿）
        t_ff_limit: 16.0 # 单关节前馈上限（N·m）：固件硬限幅 ±16，只能收紧
        params: gravity/piper_6dof.json # 参数文件（相对配置目录）
        # 每臂一份：params 写成 {left: ..., right: ...}，或用 arms.<控制器名>.params 覆盖
```

坐标系约定（米 / 弧度、`R = Rz(yaw)Ry(pitch)Rx(roll)`、法兰系）、求解器与失败语义见
[robot-pipeline 位姿动作](../wiki/design/robot_pipeline_cartesian.md)。

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
自动恢复。按臂子集的下发（`layout` + `arms`，如 `layout="joint+gripper"`）同款让位（同样回 409）。
