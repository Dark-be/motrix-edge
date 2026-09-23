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

"""server/rpent —— RPent 兼容的 RPC 面（``POST /call``），供**外部 agent** 驱动 edge。

**定位**：RPent 一类外部 agent（LLM 当大脑、冻结 VLA 当小脑，多轮工具调用把原语组合成长程
操作）只发**意图**——相对位移 / 夹爪开合 / 关节复位 / 动作块——执行与观测归 edge。设计见
``wiki/design/motrix_edge_rpent_bridge.md``。

**模块划分**（本包是唯一实现，别处不要再写一份）：

| 模块       | 职责                                                                       |
| ---------- | -------------------------------------------------------------------------- |
| `wire`     | RPC 线缆编解码：ndarray / numpy 标量的 ``__ndarray__`` / ``__npscalar__`` tag |
| `coerce`   | 观测 / 入参规范化（JPEG → RGB、动作块 → 矩阵、调用形态解析、角度包装）     |
| `errors`   | `RpentError`（HTTP 层据此回 ``ok=false`` 信封 + ``kind`` 分类）              |
| `layout`   | 外部动作块布局（``action_layout``）与几何换算（rot6d / rpy / 四元数 / 夹爪域） |
| `node_view`| `EdgeNodeView`：`EdgeNode` 的**窄读接口**（adapter / 帧缓存 / 实测频率 / 会话） |
| `settle`   | 到位等待：`SettleConfig`（部署配置 + 逐次覆盖）、容差判定、回执              |
| `service`  | `RpentService`：方法分派、租约、自描述、观测载荷、``env.*`` 原语与动作块下发 |

**协议（与 RPent ``rpent/utils/rpc/http_rpc.py`` 对称）**

- 单端点 ``POST /call``，请求体 ``{"method", "args", "kwargs", "session_id"}``；
- 响应**始终 HTTP 200**：``{"ok": true, "result": ...}`` / ``{"ok": false, "error", "traceback"}``
  （本服务额外带 ``kind`` 便于分类：``lease`` / ``state`` / ``argument`` / ``unsupported`` /
  ``unknown_method`` / ``internal``）；
- numpy 编码见 :mod:`~motrix_edge.server.rpent.wire`；
- 方法名 ``<facade>.<verb>``；本服务实现 ``env.*`` 子集 + ``healthz`` + 会话 RPC 空实现。
  **``shutdown`` 不注册**：edge 生命周期归 node / Console，不给 agent 关服的能力。

**方法映射（只转发，不复制语义）**

| RPent 方法                                        | edge 承接                                    |
| ------------------------------------------------- | -------------------------------------------- |
| ``env.get_env_meta`` / ``env.get_camera_meta``    | adapter 能力自描述 + 观测缓存分辨率（**免租约**） |
| ``env.get_observation`` / ``env.get_robot_state`` | ``adapter.observe()`` 原图（``image_source: native``）/ 降采样帧 |
| ``env.reset``                                     | 命令通道 ``robot/reset``（含租约与回执）     |
| ``env.move_delta`` / ``env.rotate_delta``         | ``adapter.rollout(pose_delta)``（机器人侧叠加增量） |
| ``env.set_gripper``                               | ``adapter.rollout(joint)``（基座取关节段目标，其余维不变） |
| ``env.recover_joint_posture``                     | 关节回 home（``HOME["joint"]``）+ 保持夹爪 |
| ``env.step`` / ``env.chunk_step``                 | 逐帧 ``adapter.rollout(...)``（按控制频率节奏） |
| ``env.get_task_language``                         | 推理会话的 prompt（无会话 → ``None``）       |

**租约**：观测方法与写方法须持有 Edge 级活跃租约（与 ``/v1/commands`` / ``/v1/preview`` 同规则）。
RPent 不认 ``X-Lease-Id`` 头，故租约 id 由本服务解析：配置 ``server.rpent.lease_id``（固定）
优先，否则取 ``LeaseManager`` 当前活跃租约；两处都没有 → 回 ``ok=false`` + ``kind="lease"``。
``healthz`` / ``env.get_env_meta`` / ``env.get_camera_meta`` **免租约**，让 agent 在签发租约前
就能完成握手与能力协商。

**注意（RPent 连接即 ``env.reset``）**：``env.reset`` 不是免租约方法，故 agent 连上来之前
**必须先有活跃租约**（Console 签发 / 固定 ``lease_id``），且租约 TTL 要覆盖整轮 run
（真机 LLM 循环常见 10–40 分钟）。``env.get_env_meta`` 的 ``lease`` 段带
``expires_at`` / ``expires_in_s`` / ``required``，供 agent 启动自检。

**边界**：本服务不持有任何硬件状态，也不新增机器人侧契约——所有读写都经 node /
``CommandService`` / adapter，故与原生面（``/v1/commands`` / ``/v1/preview`` / ``/v1/infers``）
共享同一份真相，不会出现第二套。

**外部动作块布局转换（可选）**

外部 agent 的动作块布局往往与 edge 的 ``action_dim`` 不一致（如 RPent dual-Franka 的 20 维
``[L_xyz(3), L_rot6d(6), L_grip(1), R_xyz(3), R_rot6d(6), R_grip(1)]``）。配
``server.rpent.action_layout: rpent/dual_franka`` 后，``env.step`` / ``env.chunk_step`` 先做**逐帧**
转换再下发（IK 仍归机器人侧，换算助手见 :mod:`~motrix_edge.server.rpent.layout`）：

- 姿态：``rot6d``（旋转矩阵前两列，列主序）→ 旋转矩阵（第三列 = 前两列叉乘，带正交化）→ ``rpy``
  （约定 ``R = Rz(yaw) @ Ry(pitch) @ Rx(roll)``）；
- 夹爪：对方 ``+1 = 张开 / -1 = 闭合`` → edge ``[0, 1]``（``(g + 1) / 2``）；
- 臂映射：按**臂名**对齐到 edge 启用臂；布局里而 edge 未启用的臂忽略；edge 启用而布局未覆盖的臂
  **保持当前位姿**（需 ``pose_dim > 0``）；
- 语义：双 Franka 的块是**绝对 TCP 位姿**（非增量），``action_scale`` 是机器人侧的每步限幅 →
  edge 不做缩放；
- ``server.rpent.dry_run: true`` → **任何下发都被拦住**（`step` / `chunk_step` 只回转换结果；
  `reset` / 四条写原语回 `sent: false` + `reached: null` + `reason: dry_run`；`_push_action` 兜底
  报错），真机联调先对数值、机器人不动；是否 dry-run 可从 `env.get_env_meta` 的 `settle.dry_run` 看出。

**到位等待（settle）**：写原语下发后阻塞到机器人到位（参数 / 回执见
:mod:`~motrix_edge.server.rpent.settle`）。容差按 **MIT 实际稳态误差**标定——底层只有 P/D、
**无重力 / 力矩前馈**，故“设定什么关节就是什么关节”并不成立。
"""

from __future__ import annotations

from .errors import RpentError
from .layout import (
    CARTESIAN_ACTION_DIM_PER_ARM,
    LAYOUT_RPENT_DUAL_FRANKA,
    RPENT_BLOCK_DIM,
    matrix_to_quat,
    matrix_to_rot6d,
    matrix_to_rpy,
    resolve_layout,
    rot6d_to_matrix,
    rpent_gripper_to_edge,
    rpy_to_matrix,
)
from .service import RpentService
from .settle import SettleConfig
from .wire import from_wire, to_wire

__all__ = [
    "CARTESIAN_ACTION_DIM_PER_ARM",
    "LAYOUT_RPENT_DUAL_FRANKA",
    "RPENT_BLOCK_DIM",
    "RpentError",
    "RpentService",
    "SettleConfig",
    "from_wire",
    "matrix_to_quat",
    "matrix_to_rpy",
    "matrix_to_rot6d",
    "resolve_layout",
    "rot6d_to_matrix",
    "rpent_gripper_to_edge",
    "rpy_to_matrix",
    "to_wire",
]
