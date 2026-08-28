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
- **服务器**：SDK 进程（``scripts/test_robot_sdk.py``）——接收调用。

端点一览（前缀 ``/v1``）：

| 方法 | 路径                     | 请求 body              | 响应 body                                      |
| ---- | ------------------------ | ---------------------- | ---------------------------------------------- |
| POST | ``/v1/discover``         | —                      | ``{status, robot}``（robot 自描述见下）    |
| GET  | ``/v1/health``           | —                      | ``{ok, detail}``                               |
| POST | ``/v1/reset``            | —                      | ``{status}``                                   |
| POST | ``/v1/execute``          | ``{action}``           | ``{status}``                                   |
| POST | ``/v1/rollout``          | ``{action: [dim]}``    | ``{status}``                                   |
| POST | ``/v1/teleop``           | ``{enabled}``          | ``{status}``                                   |
| POST | ``/v1/safe_stop``        | —                      | ``{status}``                                   |
| GET  | ``/v1/data_status``      | —                      | ``{data_dir, data_files, running}``            |
| GET  | ``/v1/capture/status``   | —                      | ``{running, operator, task_name, meta}``       |
| POST | ``/v1/capture/sync``     | ``{meta}``             | ``{status}``                                   |
| POST | ``/v1/capture/start``    | —                      | ``{status}``                                   |
| POST | ``/v1/capture/end``      | —                      | ``{status}``                                   |

- ``/v1/discover``：机器人进程**自描述探活**（不初始化）——声明身份与它支持被哪些
  adapter 类型操作（``supported_adapters``），edge 据此把 adapter 标记为可用并选择。
- ``status`` 取值 ``accepted`` 表示指令已被 SDK 接受。
"""

from __future__ import annotations

# ---- 端点路径 ----
PATH_DISCOVER = "/v1/discover"  # 机器人进程自描述探活（不初始化；声明支持的 adapter 类型）
PATH_HEALTH = "/v1/health"  # 健康检查
PATH_RESET = "/v1/reset"  # 程序复位到 home
PATH_EXECUTE = "/v1/execute"  # 直接下发 raw 动作
PATH_ROLLOUT = "/v1/rollout"  # 推理闭环：模型 action
PATH_TELEOP = "/v1/teleop"  # 设置遥操作开关（true=遥操作 / false=程控）
PATH_SAFE_STOP = "/v1/safe_stop"  # 安全停止
PATH_DATA_STATUS = "/v1/data_status"  # 采集数据状态查询（数据由进程自维护）
PATH_CAPTURE_STATUS = "/v1/capture/status"  # 采集状态查询（采集员 / 任务名等元信息）
PATH_CAPTURE_SYNC = "/v1/capture/sync"  # 同步采集元信息到进程（保存数据时附加）
PATH_CAPTURE_START = "/v1/capture/start"  # 开始一轮采集（episode 开始）
PATH_CAPTURE_END = "/v1/capture/end"  # 结束一轮采集（episode 结束）

# ---- 请求 body 字段 ----
FIELD_ACTION = "action"  # execute / rollout：动作数据
FIELD_TELEOP_ENABLED = "enabled"  # teleop：是否启用遥操作（bool）
FIELD_DATA_DIR = "data_dir"  # data_status：数据目录（SDK 进程自维护；edge 只收集 / 上传）
FIELD_META = "meta"  # capture sync：采集元信息（dict，保存数据时附加）
FIELD_OPERATOR = "operator"  # capture status：采集员姓名
FIELD_TASK_NAME = "task_name"  # capture status：任务名称

# ---- 响应 body 字段 ----
FIELD_STATUS = "status"  # 指令是否被接受（accepted）
FIELD_OK = "ok"  # health：是否健康
FIELD_DETAIL = "detail"  # health：详情
FIELD_ROBOT = "robot"  # discover：机器人自描述块
FIELD_NAME = "name"  # robot：名称（展示）
FIELD_TYPE = "type"  # robot：adapter 类型（entry point 名，用于实例化）
FIELD_SUPPORTED_ADAPTERS = "supported_adapters"  # robot：声明支持的 adapter 类型
FIELD_ROBOT_MODEL_ID = "robot_model_id"  # connect / robot：机器人型号
FIELD_ROBOT_MODEL_VERSION = "robot_model_version"  # robot：机器人型号版本
FIELD_ACTION_DIM = "action_dim"  # robot：动作维度
FIELD_OBSERVATION_KEYS = "observation_keys"  # robot：观测键布局（含 observations/images/<cam>）
FIELD_CONTROLLERS = "controllers"  # robot：控制器列表
FIELD_SENSORS = "sensors"  # robot：传感器列表
FIELD_CAPABILITIES = "capabilities"  # robot：能力 dict（capture / execute / streaming）
FIELD_ENDPOINT = "endpoint"  # robot：SDK HTTP 指令地址
FIELD_SHM_NAME = "shm_name"  # robot：观测共享内存通道名
FIELD_RUNNING = "running"  # connect / data_status / robot：SDK 是否运行
FIELD_DATA_FILES = "data_files"  # data_status：本次采集得到的数据列表

# ---- 状态值 ----
VALUE_STATUS_ACCEPTED = "accepted"

__all__ = [
    "FIELD_ACTION",
    "FIELD_ACTION_DIM",
    "FIELD_CAPABILITIES",
    "FIELD_CONTROLLERS",
    "FIELD_DATA_DIR",
    "FIELD_DATA_FILES",
    "FIELD_DETAIL",
    "FIELD_ENDPOINT",
    "FIELD_META",
    "FIELD_NAME",
    "FIELD_OBSERVATION_KEYS",
    "FIELD_OK",
    "FIELD_OPERATOR",
    "FIELD_ROBOT",
    "FIELD_ROBOT_MODEL_ID",
    "FIELD_ROBOT_MODEL_VERSION",
    "FIELD_RUNNING",
    "FIELD_SENSORS",
    "FIELD_SHM_NAME",
    "FIELD_STATUS",
    "FIELD_SUPPORTED_ADAPTERS",
    "FIELD_TASK_NAME",
    "FIELD_TELEOP_ENABLED",
    "FIELD_TYPE",
    "PATH_CAPTURE_END",
    "PATH_CAPTURE_START",
    "PATH_CAPTURE_STATUS",
    "PATH_CAPTURE_SYNC",
    "PATH_DATA_STATUS",
    "PATH_DISCOVER",
    "PATH_EXECUTE",
    "PATH_HEALTH",
    "PATH_RESET",
    "PATH_ROLLOUT",
    "PATH_SAFE_STOP",
    "PATH_TELEOP",
    "VALUE_STATUS_ACCEPTED",
]
