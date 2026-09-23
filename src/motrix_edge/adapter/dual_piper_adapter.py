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

"""DualPiperAdapter —— 双臂 Piper 机器人适配器（HTTP + 共享内存薄客户端）。

继承 ``HttpShmAdapter`` 的共享实现（HTTP 指令下行 + 共享内存观测上行 + 状态查询），
本类只声明类常量：身份（discover 解析传入，缺省回退类常量）、能力（动作维度 / 相机
布局 / 支持的 ``AdapterCapability``）、连接参数（SDK URL / 共享内存名 / 超时），以及
双臂按 **三个动作空间**取值（每个空间的值都按臂等长展开）：
``joint`` 每臂 6 关节角（双臂 12）、``pose`` 每臂 ``xyz + rpy``（双臂 12）、
``gripper`` 每臂 1 夹爪（双臂 2）。

相机布局：``cam_head`` / ``cam_left_wrist`` / ``cam_right_wrist``（640×480）。运行时可由
Edge 配置（``adapter`` 段）裁剪——``configure()`` 只启用指定臂 / 相机，未启用臂用**同空间**
``HOME`` 填充（``pose`` 未启用臂时直接拒绝，不编位姿）。
"""

from motrix_edge.adapter.base import ActionSpace, AdapterCapability
from motrix_edge.adapter.http_shm_adapter import HttpShmAdapter

# ---- 双臂布局（各动作空间按臂等长展开；物理顺序 = DUAL_ARM_NAMES）----
# 双臂机型共享同一组布局（DualPiperAdapter / TestRobotAdapter），避免多处重复声明：
#   joint      → 每臂 6 关节角      → 双臂 12
#   pose       → 每臂 xyz + rpy     → 双臂 12
#   pose_delta → 每臂 xyz + rpy 增量 → 双臂 12（与 pose 同形，语义不同）
#   gripper    → 每臂 1 夹爪        → 双臂 2
DUAL_ARM_NAMES = ("left", "right")
DUAL_ARM_ACTION_DIM_PER_ARM = {
    ActionSpace.JOINT.value: 6,
    ActionSpace.POSE.value: 6,
    ActionSpace.POSE_DELTA.value: 6,
    ActionSpace.GRIPPER.value: 1,
}
DUAL_ARM_ACTION_DIM = {space: per_arm * len(DUAL_ARM_NAMES) for space, per_arm in DUAL_ARM_ACTION_DIM_PER_ARM.items()}
# 未启用臂的填充值（**同空间** home；``pose`` / ``pose_delta`` 有意不给 home ——
# 编一个位姿比拒绝更危险；``pose_delta`` 的全臂要求见 ``_require_full_arms_for_cartesian``）
DUAL_ARM_HOME = {
    ActionSpace.JOINT.value: [0.0] * DUAL_ARM_ACTION_DIM[ActionSpace.JOINT.value],
    ActionSpace.GRIPPER.value: [1.0] * DUAL_ARM_ACTION_DIM[ActionSpace.GRIPPER.value],  # 1 = 张开
}


class DualPiperAdapter(HttpShmAdapter):
    # ---- 身份（discover 解析传入；缺省回退类常量）----
    NAME = "dual_piper"  # 实例名（debug_print 前缀）
    ADAPTER_TYPE = "dual_piper"  # 本 adapter 的 entry point 类型

    # ---- 能力 / 连接参数（类级常量，自包含，不随 discover 传输）----
    ROBOT_MODEL_ID = "dual-piper"
    ROBOT_MODEL_VERSION = "0.0.0"
    # 各动作空间的每臂 / 全臂维度（joint / pose / pose_delta 每臂 6、gripper 每臂 1）
    ACTION_DIM_PER_ARM = DUAL_ARM_ACTION_DIM_PER_ARM
    # 支持的动作空间：关节 + 末端位姿（绝对 / 增量）+ 夹爪（每个空间只表达一件事）。
    # ``pose`` / ``pose_delta`` 的每臂 6 维 = xyz + rpy，由**机器人侧**用与位姿观测同一套运动学
    # 解算成关节目标（IK，失败回 422 且不改目标），再走 MIT 关节通路——**不经 move_p**，不引入
    # 第二套运动模式；``pose_delta`` 的基准是**关节段目标**的正解位姿（不是实测位姿，见
    # wiki/design/robot_pipeline_cartesian.md「位姿增量」）。
    ACTION_SPACES = (ActionSpace.JOINT, ActionSpace.POSE, ActionSpace.POSE_DELTA, ActionSpace.GRIPPER)
    # 位姿坐标系：下发的 pose 目标与 ``observations/pose`` / ``observations/pose_target``（每臂 6
    # 维）由**同一套运动学**给出（法兰系，见 robot-pipeline/src/robot/kinematics），故可直接比对。
    POSE_FRAME = "flange"
    # 臂布局（基类 configure / _select_arm_segments / _expand_action 消费）
    ARM_NAMES = DUAL_ARM_NAMES  # 物理顺序臂名
    HOME = DUAL_ARM_HOME  # 未启用臂填充（按空间）
    DEFAULT_ENABLED_ARMS = DUAL_ARM_NAMES  # 缺省启用全部臂；Edge 配置可裁剪
    # 相机布局：{相机名: 分辨率 (width, height)}（SDK 产出 raw RGB；observe 编码 JPEG 原图）
    IMAGES: dict[str, tuple[int, int]] = {
        "cam_head": (640, 480),
        "cam_left_wrist": (640, 480),
        "cam_right_wrist": (640, 480),
    }

    CAPABILITIES: dict[AdapterCapability, bool] = {
        AdapterCapability.CAPTURE: True,
        AdapterCapability.EXECUTE: True,
        AdapterCapability.STREAMING: True,
    }

    # 中间件连接参数（与双臂 Piper SDK 进程约定）
    SDK_URL = "http://127.0.0.1:8090"
    SHM_NAME = "dual_piper_obs"
    HTTP_TIMEOUT = 3.0
