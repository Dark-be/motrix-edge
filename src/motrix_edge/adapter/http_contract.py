# Confidential Information of Motphys. Not for disclosure or distribution without Motphys's prior
# written consent.
#
# This software contains code, techniques and know-how which is confidential and proprietary to
# Motphys.
#
# Product and Trade Secret source code contains trade secrets of Motphys.
#
# Copyright (C) 2020-2026 Motphys Technology Co., Ltd. All Rights Reserved.
#
# This software belongs to the Intellectual Property of Motphys. Use of this software is subject to
# the terms and conditions in the license file accompanying. You may not use this software except
# in compliance with the license file.

"""http_contract —— adapter ↔ 机器人 SDK 进程的 HTTP 指令契约。

与共享内存观测契约（``shm_contract.py``）对称，本模块是「指令下行」通道（adapter → SDK）的
**HTTP API 约定**：端点路径 + 请求 / 响应 body 字段，两端**单点定义**，避免硬编码漂移。

- **客户端**：Edge adapter 中间件（``test_adapter.py``）——发起调用。
- **服务器**：SDK 进程（``robot-pipeline/src/server/contract_server.py``）——接收调用。

端点一览（前缀 ``/v1``）：

| 方法 | 路径                     | 请求 body              | 响应 body                                      |
| ---- | ------------------------ | ---------------------- | ---------------------------------------------- |
| POST | ``/v1/discover``         | —                      | ``{status, robot}``（robot 自描述见下）    |
| GET  | ``/v1/health``           | —                      | ``{ok, detail, control_hz, measured_hz}``      |
| POST | ``/v1/reset``            | —                      | ``{status}``                                   |
| POST | ``/v1/execute``          | ``{action, action_space?}`` | ``{status}``                                   |
| POST | ``/v1/rollout``          | ``{action, action_space?}`` | ``{status}``                                   |
| POST | ``/v1/teleop``           | ``{enabled, mode?}``   | ``{status}``                                   |
| POST | ``/v1/safe_stop``        | —                      | ``{status}``                                   |
| GET  | ``/v1/capture/status``   | —                      | 采集状态（运行位 / 元信息 / 数据目录）        |
| POST | ``/v1/capture/sync``     | ``{meta}``             | ``{status}``                                   |
| POST | ``/v1/capture/start``    | —                      | ``{status}``                                   |
| POST | ``/v1/capture/end``      | —                      | ``{status}``                                   |

- ``/v1/discover``：机器人进程**自描述探活**（不初始化）——声明身份与它支持被哪些
  adapter 类型操作（``supported_adapters``），edge 据此把 adapter 标记为可用并选择。
- ``/v1/capture/status`` 响应字段：``running``（进程是否正在采集）/ ``meta``（``capture sync``
  同步的元信息全集）/ ``data_dir``——**合并**了原 ``/v1/data_status``（已删除，避免两处
  状态不一致）；数据文件列表不在本端点：数据的扫描 / 选择 / 打包见
  [上传会话（UploadSession）](../../../wiki/design/motrix_edge_upload_session.md)。
- **元信息只有 ``meta`` 一个载体**：采集员 / 任务名等是 ``meta`` 里的键（分类可拓展），
  不再另设同义顶层字段——消费方按需取 ``meta["operator"]`` / ``meta["task_name"]``。
- ``status`` 取值 ``accepted`` 表示指令已被 SDK 接受。
- ``/v1/teleop`` 的 ``mode``（取值 ``absolute`` / ``delta``，缺省 ``absolute``）：``absolute``
  把主臂绝对位姿直连从臂 target（示教采集）；``delta`` 为**人工接管**——以接管瞬间的主 / 从
  位姿为锚点，只把主臂**增量**叠加到从臂 target（从臂不突变）。只发 ``{enabled}`` 的调用方
  行为不变；robot-pipeline 侧语义见
  [robot-pipeline 遥操作](../../../wiki/design/robot_pipeline_teleop.md)。
- ``/v1/execute`` 与 ``/v1/rollout`` 的 ``action_space``（取值 ``joint`` / ``pose`` /
  ``pose_delta`` / ``gripper``，缺省 ``joint``）：声明 ``action`` 的语义——``joint`` 为关节空间绝对
  目标（每臂 6 关节角），``pose`` 为末端位姿绝对目标（每臂 xyz + rpy），``pose_delta`` 为位姿
  **增量**（叠加在**关节段目标**的正解位姿上），``gripper`` 为夹爪（每臂 1）。``pose`` /
  ``pose_delta`` 由机器人侧求解器解算成关节目标（不切运动模式，仍走 MIT 关节通路）后执行。
  不传该字段的调用方行为不变；解算失败（超限位 / 不收敛）→ 422 且**不改既有目标**；位姿动作见
  [robot-pipeline 位姿动作](../../../wiki/design/robot_pipeline_cartesian.md)。
- **位姿增量叠在目标上**：``pose_delta`` 的基准是机器人自己的**关节段目标**（不是实测位姿）：
  底层 MIT 无重力前馈，实测恒落后目标一个稳态误差，以实测为基准会把误差写进新目标、逐步累积。
  目标位姿由机器人常驻发布为 ``observations/pose_target``（= ``FK(关节段目标)``），与
  ``observations/pose`` 同系可比。
"""

from __future__ import annotations

# ---- 端点路径 ----
PATH_DISCOVER = "/v1/discover"  # 机器人进程自描述探活（不初始化；声明支持的 adapter 类型）
PATH_HEALTH = "/v1/health"  # 健康检查
PATH_RESET = "/v1/reset"  # 程序复位到 home
PATH_EXECUTE = "/v1/execute"  # 直接下发 raw 动作
PATH_ROLLOUT = "/v1/rollout"  # 推理闭环：模型 action
PATH_TELEOP = "/v1/teleop"  # 设置遥操作（enabled=true 遥操作 / false 程控；mode 可选）
PATH_SAFE_STOP = "/v1/safe_stop"  # 安全停止（软停：停发指令并保持位姿，不断电）
PATH_CAPTURE_STATUS = "/v1/capture/status"  # 采集状态（运行位 / 元信息 / 数据目录）
PATH_CAPTURE_SYNC = "/v1/capture/sync"  # 同步采集元信息到进程（保存数据时附加）
PATH_CAPTURE_START = "/v1/capture/start"  # 开始一轮采集（episode 开始）
PATH_CAPTURE_END = "/v1/capture/end"  # 结束一轮采集（episode 结束）

# ---- 请求 body 字段 ----
FIELD_ACTION = "action"  # execute / rollout：动作数据
FIELD_ACTION_SPACE = "action_space"  # rollout / health：动作空间（joint | pose | pose_delta | gripper；缺省 joint）
FIELD_TELEOP_ENABLED = "enabled"  # teleop：是否启用遥操作（bool）
FIELD_TELEOP_MODE = "mode"  # teleop：遥操作映射模式（absolute | delta；缺省 absolute）
FIELD_DATA_DIR = "data_dir"  # capture status：数据目录（SDK 进程自维护；edge 只收集 / 上传）
FIELD_HEAD_SKIP = "head_skip"  # capture status：帧头跳过进度（{"skipped": n}；null = 未在跳过）
FIELD_META = "meta"  # capture sync：采集元信息（dict，保存数据时附加）

# ---- 响应 body 字段 ----
FIELD_STATUS = "status"  # 指令是否被接受（accepted）
FIELD_OK = "ok"  # health：是否健康
FIELD_DETAIL = "detail"  # health：详情
FIELD_CONTROL_HZ = "control_hz"  # health：名义控制频率（robot env 控制线程 HZ，Hz）
FIELD_MEASURED_HZ = "measured_hz"  # health：实测控制线程帧率（最近窗口，Hz）
FIELD_ROBOT = "robot"  # discover：机器人自描述块
FIELD_NAME = "name"  # robot：名称（展示）
FIELD_TYPE = "type"  # robot：adapter 类型（entry point 名，用于实例化）
FIELD_SUPPORTED_ADAPTERS = "supported_adapters"  # robot：声明支持的 adapter 类型
FIELD_ROBOT_MODEL_ID = "robot_model_id"  # connect / robot：机器人型号
FIELD_ROBOT_MODEL_VERSION = "robot_model_version"  # robot：机器人型号版本
FIELD_ACTION_DIM = "action_dim"  # robot：joint 空间维度（兼容字段）
FIELD_ACTION_DIMS = "action_dims"  # robot：各动作空间维度（{joint: 12, pose: 12, pose_delta: 12, gripper: 2}）
FIELD_OBSERVATION_KEYS = "observation_keys"  # robot：观测键布局（含 observations/images/<cam>）
FIELD_CONTROLLERS = "controllers"  # robot：控制器列表
FIELD_SENSORS = "sensors"  # robot：传感器列表
FIELD_CAPABILITIES = "capabilities"  # robot：能力 dict（capture / execute / streaming）
FIELD_ENDPOINT = "endpoint"  # robot：SDK HTTP 指令地址
FIELD_SHM_NAME = "shm_name"  # robot：观测共享内存通道名
FIELD_RUNNING = "running"  # discover / capture status / robot：是否运行（语义随端点：进程运行 / 采集进行中）

# ---- 状态值 ----
VALUE_STATUS_ACCEPTED = "accepted"

# ---- teleop 模式取值（robot 层同名常量在 robot-pipeline 的 BaseRobot.TELEOP_MODES）----
VALUE_TELEOP_MODE_ABSOLUTE = "absolute"  # 主臂绝对位姿直连从臂 target（示教采集）
VALUE_TELEOP_MODE_DELTA = "delta"  # 人工接管：锚点增量（target = slave_ref + 主臂增量）
TELEOP_MODES = (VALUE_TELEOP_MODE_ABSOLUTE, VALUE_TELEOP_MODE_DELTA)
DEFAULT_TELEOP_MODE = VALUE_TELEOP_MODE_ABSOLUTE  # 不传 mode 时保持旧行为

# ---- 动作空间取值（与 adapter/base.py 的 ActionSpace 一一对应；两端引用不硬编码）----
VALUE_ACTION_SPACE_JOINT = "joint"  # 关节空间（每臂 6 关节角，绝对目标）
VALUE_ACTION_SPACE_POSE = "pose"  # 末端位姿（每臂 xyz + rpy，绝对目标）
VALUE_ACTION_SPACE_POSE_DELTA = "pose_delta"  # 末端位姿**增量**（每臂 xyz + rpy）
VALUE_ACTION_SPACE_GRIPPER = "gripper"  # 夹爪（每臂 1，归一化 [0, 1]）
ACTION_SPACES = (
    VALUE_ACTION_SPACE_JOINT,
    VALUE_ACTION_SPACE_POSE,
    VALUE_ACTION_SPACE_POSE_DELTA,
    VALUE_ACTION_SPACE_GRIPPER,
)
DEFAULT_ACTION_SPACE = VALUE_ACTION_SPACE_JOINT  # 不发该字段时机器人按关节空间解释（向后兼容）

__all__ = [
    "ACTION_SPACES",
    "DEFAULT_ACTION_SPACE",
    "DEFAULT_TELEOP_MODE",
    "FIELD_ACTION",
    "FIELD_ACTION_DIM",
    "FIELD_ACTION_DIMS",
    "FIELD_ACTION_SPACE",
    "FIELD_CAPABILITIES",
    "FIELD_CONTROL_HZ",
    "FIELD_CONTROLLERS",
    "FIELD_DATA_DIR",
    "FIELD_HEAD_SKIP",
    "FIELD_DETAIL",
    "FIELD_ENDPOINT",
    "FIELD_MEASURED_HZ",
    "FIELD_META",
    "FIELD_NAME",
    "FIELD_OBSERVATION_KEYS",
    "FIELD_OK",
    "FIELD_ROBOT",
    "FIELD_ROBOT_MODEL_ID",
    "FIELD_ROBOT_MODEL_VERSION",
    "FIELD_RUNNING",
    "FIELD_SENSORS",
    "FIELD_SHM_NAME",
    "FIELD_STATUS",
    "FIELD_SUPPORTED_ADAPTERS",
    "FIELD_TELEOP_ENABLED",
    "FIELD_TELEOP_MODE",
    "FIELD_TYPE",
    "PATH_CAPTURE_END",
    "PATH_CAPTURE_START",
    "PATH_CAPTURE_STATUS",
    "PATH_CAPTURE_SYNC",
    "PATH_DISCOVER",
    "PATH_EXECUTE",
    "PATH_HEALTH",
    "PATH_RESET",
    "PATH_ROLLOUT",
    "PATH_SAFE_STOP",
    "PATH_TELEOP",
    "TELEOP_MODES",
    "VALUE_ACTION_SPACE_GRIPPER",
    "VALUE_ACTION_SPACE_JOINT",
    "VALUE_ACTION_SPACE_POSE",
    "VALUE_ACTION_SPACE_POSE_DELTA",
    "VALUE_STATUS_ACCEPTED",
    "VALUE_TELEOP_MODE_ABSOLUTE",
    "VALUE_TELEOP_MODE_DELTA",
]
