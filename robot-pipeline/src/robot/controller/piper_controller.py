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

import functools
import time
from typing import NamedTuple

import numpy as np
from config import resolve_config_file
from pyAgxArm import AgxArmFactory, ArmModel, PiperFW, create_agx_arm_config
from utils.data_handler import debug_print, set_log_level

from robot.gravity import (
    DEFAULT_ALPHA,
    DEFAULT_PARAMS_RELATIVE,
    T_FF_LIMIT_NM,
    GravityCompensator,
    load_gravity_params,
    resolve_arm_config,
)
from robot.kinematics import IkResult, PiperKinematics, solve_ik

from .arm_controller import ArmController

# 单个共享运动学模型（DH + 软限位 = ``PIPER_JOINT_LIMITS``，全仓唯一一份）：位姿→关节的解算、
# 关节→位姿的正解、``set_joint`` 下发前的裁切都用它，所以「解出来的」与「发下去的」永远同一范围。
# 纯 numpy、无硬件、**无实例状态**——robot 层直接调下面两个静态函数即可。
_KINEMATICS = PiperKinematics()

# MIT 位置环缺省参数：**只有 P/D**（``kp`` / ``kd``），``t_ref = 0`` = 缺省无力矩前馈、
# ``vel_ref = 0`` ——即不补重力 / 科氏 / 摩擦，也不做力控；所以关节会停在
# ``τ_gravity / kp`` 附近的平衡点（“设定什么关节就是什么关节”并不成立）。上位任何
# 「到位」判定（edge 的 ``settle`` / RPent 的 ``reached``）都得按**实测稳态误差**设容差。
#
# 重力前馈（可选，见 ``robot.gravity`` 与 wiki/design/robot_pipeline_impedance.md）：装载后
# ``t_ref`` 改为每拍的 ``α·τ̂_g(q_meas)``（仍受固件 ±16 N·m 限幅），上面那条平衡点描述因此
# 只在**未装载 / 占位参数 / 降级**时成立。
MIT_CTRL_CFG = [
    {"vel_ref": 0.0, "kp": 6.0, "kd": 0.8, "t_ref": 0.0},
    {"vel_ref": 0.0, "kp": 4.0, "kd": 1.0, "t_ref": 0.0},
    {"vel_ref": 0.0, "kp": 6.0, "kd": 0.9, "t_ref": 0.0},
    {"vel_ref": 0.0, "kp": 3.0, "kd": 0.5, "t_ref": 0.0},
    {"vel_ref": 0.0, "kp": 3.0, "kd": 0.5, "t_ref": 0.0},
    {"vel_ref": 0.0, "kp": 2.0, "kd": 0.4, "t_ref": 0.0},
]


def _scalar(value) -> float:
    """取标量（SDK 个别字段是长度 1 的序列；统一成 ``float``）。"""
    return float(np.asarray(value, dtype=np.float64).reshape(-1)[0])


class MotorState(NamedTuple):
    """一拍电机状态（6 关节，实测）：位置 rad / 速度 rad·s⁻¹ / 力矩 N·m。"""

    q: np.ndarray
    vel: np.ndarray
    tau: np.ndarray


def _require_robot(method):
    """方法装饰器：调用前统一校验 self.robot 已连接，未连接则抛出统一错误。

    避免每个方法里重复 ``if self.robot is None: raise ...``。
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if self.robot is None:
            raise RuntimeError(f"{self.name}: Piper is not set up. Call connect() first.")
        return method(self, *args, **kwargs)

    return wrapper


class PiperController(ArmController):
    """Piper 单臂控制器：**关节 / 夹爪读写 + 限位把关**；运动学是**无状态静态函数**。

    对外只有三类：

    - ``get_joint`` / ``set_joint``：唯一的一条关节通路（``set_joint`` 只给 MIT 的 ``p_des``，
      ``kp`` / ``kd`` / ``t_ff`` 用 ``MIT_CTRL_CFG`` 缺省值）；
    - ``get_gripper`` / ``set_gripper``：夹爪；
    - ``joint_to_pose(q)`` / ``pose_to_joint(pose, seed)``：**静态**运动学转换——robot 层把
      ``pose`` 目标解成关节角后，与 ``joint`` 目标走同一条控制通路（解算不进控制拍）。

    **限位只有一份**：上述静态转换与 ``set_joint`` 的裁切共用 ``_KINEMATICS.joint_limits``
    （= ``PIPER_JOINT_LIMITS``），无需任何注入；SDK 侧 ``set_joint_limits_enabled(True)``
    对越界值会报错并打印，先裁掉就不给它报错的机会。

    **不含 SDK 位姿读写**：底层硬件依赖只剩 ``get_joint`` + ``move_mit``；现场标定需要 SDK 法兰
    位姿时直接调 ``self.robot.get_flange_pose()``（``scripts/verify_cartesian.py``），
    ``move_p`` 在本层因此无路可达（与 MIT 互斥）。

    **重力前馈（可选）**：``MIT_CTRL_CFG`` 本身只有 P/D（``t_ref = 0``、``vel_ref = 0``），不补
    重力 / 科氏 / 摩擦；装载 ``robot.gravity`` 后每拍的 ``t_ff`` 改为 ``α·τ̂_g(q_meas)``（仍受固件
    ±16 N·m 限幅与异常降级）。参数未标定（占位全 0）时 ``t_ff`` 恒为 0，行为与未装载一致；因此
    ``τ_gravity / kp`` 那条平衡点描述只在**未装载 / 占位 / 降级**时成立。标定与设计见
    ``wiki/design/robot_pipeline_impedance.md``。
    """

    def __init__(self, name="piper_controller"):
        super().__init__(name)
        self.robot = None
        self.gripper = None
        self.port: str = "can0"
        self.ctrl_mode: str = "mit"  # 下发通路：``mit``（缺省，力矩环）或 ``joint``（SDK 位置速度模式）
        self.role: str = "follower"  # 角色，支持 "leader" 或 "follower"
        self._limit_warned: tuple[int, ...] | None = None  # 限位告警去重（同一组超限关节只打一条）
        # 重力前馈（可选，缺省 None = 纯位置环）：装载见 enable_gravity_compensation()
        self.gravity: GravityCompensator | None = None
        self.last_torque_ff: np.ndarray | None = None  # 最近一拍的 t_ff（可观测 / 现场验收）
        self._gravity_warned: str | None = None  # 前馈降级 / 限幅告警去重（同一原因只打一条）

    def connect(
        self,
        port: str = "can0",
        ctrl_mode: str = "mit",
        role: str = "follower",
        firmware: str = PiperFW.V188,
        gravity: dict | None = None,
    ):
        self.port = port
        self.ctrl_mode = ctrl_mode

        cfg = create_agx_arm_config(robot=ArmModel.PIPER, firmeware_version=firmware, channel=port)
        self.robot = AgxArmFactory.create_arm(cfg)
        self.robot.set_joint_limits_enabled(True)

        self.gripper = self.robot.init_effector(self.robot.OPTIONS.EFFECTOR.AGX_GRIPPER)

        self.robot.connect(start_read_thread=True)
        self.role = role

        if role == "leader":
            self.robot.set_leader_mode()
            debug_print(self.name, f"Connected to Piper on port {port} as LEADER", "INFO")
        elif role == "follower":
            self.robot.set_follower_mode()
            while not self.robot.enable():
                time.sleep(0.01)
            debug_print(self.name, f"Connected to Piper on port {port} as FOLLOWER", "INFO")
        else:
            debug_print(self.name, f"Connected to Piper on port {port}, ctrl_mode={ctrl_mode}", "INFO")

        # 重力前馈：只对**执行**臂有意义（主臂只读，不会被 set_joint 下发）；配置缺省不装载。
        if gravity:
            self.enable_gravity_compensation(gravity)

    @_require_robot
    def set_leader_mode(self):
        """切换为 Leader（主臂）模式：之后用 get_leader_joint_angles() 读取主臂关节。"""
        self.robot.set_leader_mode()
        self.role = "leader"
        debug_print(self.name, f"Piper on port {self.port} switched to leader mode", "INFO")

    @_require_robot
    def set_follower_mode(self):
        """切换为 Follower（从臂）模式：接收总线上 Leader（主臂）的角度用于跟随。"""
        self.robot.set_follower_mode()
        self.role = "follower"
        debug_print(self.name, f"Piper on port {self.port} switched to follower mode", "INFO")

    @_require_robot
    def get_leader_joint_angles(self):
        angles = self.robot.get_leader_joint_angles()
        if angles is None:
            debug_print(self.name, "Failed to get leader joint angles")
            return None
        return np.array(angles.msg)

    @_require_robot
    def move_leader_to_home(self):
        self.robot.move_leader_to_home()
        debug_print(self.name, "Leader moving to home (0 point)", "INFO")

    @_require_robot
    def restore_leader_drag_mode(self):
        self.robot.restore_leader_drag_mode()
        debug_print(self.name, "Leader restored to zero-gravity drag mode", "INFO")

    @_require_robot
    def set_speed_percent(self, percent: int = 100):
        """设置运行速度百分比（位置速度模式 move_j/move_p/move_l/move_c 生效，范围 0~100）。"""
        self.robot.set_speed_percent(percent)
        debug_print(self.name, f"Set speed percent to {percent}", "INFO")

    @_require_robot
    def get_arm_status(self):
        return self.robot.get_arm_status()

    def disconnect(self):
        if self.robot is not None:
            self.robot.disconnect()
            debug_print(self.name, f"Disconnected from Piper on port {self.port}", "INFO")
            self.robot = None

    @_require_robot
    def get_joint(self):
        joint_angles = None
        if self.role == "leader":
            joint_angles = self.robot.get_leader_joint_angles()
        elif self.role == "follower":
            joint_angles = self.robot.get_joint_angles()

        if joint_angles is None:
            debug_print(self.name, "Failed to get joint angles")
            return None
        return np.array(joint_angles.msg)

    @_require_robot
    def get_motor_states(self) -> MotorState | None:
        """读**一拍**电机状态（6 关节的实测 ``q`` / ``vel`` / ``tau``）；任一关节拿不到 → None。

        读的是 SDK 后台读取线程已解析好的**缓存帧**（``connect(start_read_thread=True)``），
        不是请求 / 应答——所以可以在控制拍里每拍取（``get_motor_states(i).msg.torque``，i 从 1 起）。
        ``tau`` 是重力标定的唯一依据（``simulate`` 需要「实测 q + 实测 τ」同拍配对）；
        标定脚本 ``scripts/verify_gravity.py`` 也走这里，不再自己摸 SDK。
        """
        q = np.zeros(6, dtype=np.float64)
        vel = np.zeros(6, dtype=np.float64)
        tau = np.zeros(6, dtype=np.float64)
        for index in range(6):
            state = self.robot.get_motor_states(index + 1)
            if state is None:
                return None
            q[index] = _scalar(state.msg.position)
            vel[index] = _scalar(state.msg.velocity)
            tau[index] = _scalar(state.msg.torque)
        return MotorState(q=q, vel=vel, tau=tau)

    # ---- 运动学转换（**静态纯函数**，无实例状态；robot 层调用）------------------------
    @staticmethod
    def joint_to_pose(q: np.ndarray) -> np.ndarray:
        """正解：关节角 → 末端位姿 ``[x, y, z, roll, pitch, yaw]``（米 / 弧度）。

        与 ``pose_to_joint()`` 共用同一个运动学模型，所以「读到的位姿」与「下发的目标」同系可闭合。
        """
        values = np.asarray(q, dtype=np.float64).reshape(-1)
        return _KINEMATICS.pose(values[: _KINEMATICS.DOF])

    @staticmethod
    def pose_to_joint(
        pose: np.ndarray,
        seed: np.ndarray | None = None,
        fallback_seeds: tuple[np.ndarray, ...] = (),
        **ik_config,
    ) -> IkResult:
        """位姿 → 关节目标（**只解算、不下发**）：调用方拿 ``IkResult.q`` 去写控制目标，
        之后逐拍 ``set_joint()``——位姿目标与关节目标因此在控制链路上走**同一条**通路，
        且解算只发生在这个调用里（不进控制拍）。

        解算失败不抛错（返回 ``ok=False`` + 原因，由调用方决定拒绝还是保持原目标）；
        ``seed`` 缺省零位形（离当前位形越近，解越“就近”），``ik_config`` 可覆盖
        ``solve_ik`` 的缺省参数（``pos_tol`` / ``rot_tol`` / ``max_iters`` / ``damping``…）。
        """
        start = np.zeros(_KINEMATICS.DOF) if seed is None else seed
        return solve_ik(_KINEMATICS, pose, start, fallback_seeds=tuple(fallback_seeds), **ik_config)

    @_require_robot
    def get_gripper(self):
        gripper_state = None
        if self.role == "leader":
            gripper_state = self.gripper.get_gripper_ctrl_states()
        elif self.role == "follower":
            gripper_state = self.gripper.get_gripper_status()

        if gripper_state is None:
            debug_print(self.name, "Failed to get gripper status")
            return None
        return np.clip(gripper_state.msg.value * 10, 0, 1)

    @_require_robot
    def set_joint(self, joint: np.ndarray, torque_ff: np.ndarray | None = None):
        """下发关节命令（MIT：``p_des`` + 固定 ``kp`` / ``kd``）。

        ``torque_ff`` = 可选的**前馈力矩**（6 维，Nm）：显式传入优先；不传则用**重力前馈**
        （已装载时每拍取实测 ``q`` 算 ``α·τ̂_g(q)``），两者都没有则用 MIT 配置的 ``t_ref``（= 0）。
        与 ``move_p`` 无关：底层始终只有 MIT 这一条关节通路（``move_mit`` 与 ``move_j`` / ``move_p``
        是 SDK 的互斥运动模式，不交替下发）。

        **下发前逐关节裁到软限位**（``_KINEMATICS.joint_limits``，与解算/正解同一份表）：SDK 的
        ``set_joint_limits_enabled(True)`` 对越界值会报错并打印，先裁切就不给它报错的机会；
        确实裁到时打一条 WARNING（同一组超限关节只打一条，30Hz 调用不会刷屏）。
        ⚠️ 会**就地修改**传入数组（既有语义；调用方需传 ``copy()`` 以免改到自己的状态）。
        """
        if joint.shape[0] != 6:
            debug_print(self.name, "set_joint() input size should be 6", "ERROR")
            return
        if torque_ff is not None:
            torque_ff = np.asarray(torque_ff, dtype=np.float64).reshape(-1)
            if torque_ff.shape[0] != 6:
                debug_print(self.name, "set_joint() torque_ff size should be 6", "ERROR")
                return
        elif self.gravity is not None:
            # 重力前馈：占位 / 停用时不取读数（零开销），异常时返回全 0 + 告警一条
            torque_ff = self._gravity_torque_ff()
        # 显式限位判断（越界值一帧都不下发给 SDK）
        limits = _KINEMATICS.joint_limits
        clipped = np.clip(joint, limits[:, 0], limits[:, 1])
        if not np.array_equal(clipped, joint):
            self._warn_joint_limits(joint, clipped)
            joint[:] = clipped
        else:
            self._limit_warned = None  # 回到限位内：下次超限重新告警一条

        if self.ctrl_mode == "mit":
            for i in range(6):
                self.robot.move_mit(
                    joint_index=i + 1,
                    p_des=joint[i],
                    v_des=MIT_CTRL_CFG[i]["vel_ref"],
                    kp=MIT_CTRL_CFG[i]["kp"],
                    kd=MIT_CTRL_CFG[i]["kd"],
                    t_ff=MIT_CTRL_CFG[i]["t_ref"] if torque_ff is None else float(torque_ff[i]),
                )

        elif self.ctrl_mode == "joint":
            self.robot.move_j(joint.tolist())

        self.last_torque_ff = None if torque_ff is None else np.asarray(torque_ff, dtype=np.float64).copy()
        debug_print(self.name, f"set joint to {joint}", "DEBUG")

    def _warn_joint_limits(self, requested: np.ndarray, clipped: np.ndarray) -> None:
        """关节目标超出软限位：告警一条（**同一组超限关节只打一条**，避免 30Hz 刷屏）。

        日志里同时给原始值与裁剪后的值——现场一眼能看出是「模型给的目标越界」而不是「机械臂没动」。
        """
        over = tuple(index for index in range(6) if requested[index] != clipped[index])
        if over == self._limit_warned:
            return
        self._limit_warned = over
        names = [f"j{index + 1}" for index in over]
        debug_print(
            self.name,
            f"joint target clipped to limits on {names}: "
            f"{np.round(np.asarray(requested, dtype=np.float64), 4).tolist()} -> "
            f"{np.round(np.asarray(clipped, dtype=np.float64), 4).tolist()}",
            "WARNING",
        )

    # ---- 重力前馈（可选：t_ff = α·τ̂_g(q_meas)，见 wiki/design/robot_pipeline_impedance.md）----
    def enable_gravity_compensation(self, section: dict | None, *, params: str | None = None) -> bool:
        """按 ``robot.gravity`` 配置装载重力前馈；**任何失败都降级为「无前馈」**并告警一条。

        配置形状（``robot.gravity``；**每个臂一份参数**，双臂不能共用）：

        - ``enabled``（缺省 true）、``alpha``（缺省 1.0）、``t_ff_limit``（缺省 16 N·m，只能收紧）；
        - ``params``：参数文件路径（相对配置目录）；可写成字符串（各臂共用）或
          ``{<臂名>: 路径, default: 路径}``（每臂一份）；缺省 ``gravity/piper_6dof.json``；
        - ``arms.<控制器名>``：该臂的覆盖（如 ``arms.left.params``）。

        返回是否装载了模型（**占位参数也返回 True**，但 ``GravityCompensator.active`` 为 False）。
        标定前默认指向占位文件（全 0）→ ``t_ff`` 恒为 0，行为与未补偿完全一致。
        """
        self.gravity = None
        resolved = resolve_arm_config(section or {}, self.name)
        if not resolved.get("enabled", True):
            debug_print(self.name, "重力前馈未启用（robot.gravity.enabled = false）：t_ff 恒为 0", "INFO")
            return False
        path = resolve_config_file(str(params or resolved.get("params") or DEFAULT_PARAMS_RELATIVE))
        limit = float(resolved.get("t_ff_limit", T_FF_LIMIT_NM))
        if limit > T_FF_LIMIT_NM:
            debug_print(
                self.name,
                f"重力前馈上限配置 {limit:g} N·m > 固件限幅 {T_FF_LIMIT_NM:g} N·m，已收紧到固件限幅",
                "WARNING",
            )
        try:
            model = load_gravity_params(path)
        except Exception as exc:  # noqa: BLE001 参数不可用不是致命错误：不装前馈也要能跑
            debug_print(self.name, f"重力前馈未启用（参数装载失败：{exc}）：t_ff 恒为 0", "WARNING")
            return False
        self.gravity = GravityCompensator(
            model, alpha=float(resolved.get("alpha", DEFAULT_ALPHA)), limit=limit, enabled=True
        )
        debug_print(
            self.name,
            f"重力前馈已装载：{model.describe()}；α={self.gravity.alpha:g}，限幅 ±{self.gravity.limit:g} N·m",
            "WARNING" if model.is_placeholder else "INFO",
        )
        return True

    def gravity_status(self) -> dict | None:
        """重力前馈可观测状态（健康检查 / 现场验收）：未装载 → None。"""
        if self.gravity is None:
            return None
        return {"controller": self.name, **self.gravity.status()}

    def _gravity_torque_ff(self) -> np.ndarray:
        """取本拍重力前馈（6 维，N·m）。

        占位 / 停用 / ``α=0``：**不取电机状态**，直接返回全 0（零开销）；读数不可用或模型异常：
        返回全 0 本拍退回纯位置环，并告警一条（同一原因只打一条）。
        """
        assert self.gravity is not None
        if not self.gravity.active:
            return self.gravity.torque(np.zeros(6, dtype=np.float64))
        states = self.get_motor_states() if self.robot is not None else None
        tau = self.gravity.note_failure("电机状态不可用") if states is None else self.gravity.torque(states.q)
        self._warn_gravity_note()
        return tau

    def _warn_gravity_note(self) -> None:
        """前馈降级 / 限幅时告警一条（**只在原因变化时**打印，30 Hz 不刷屏）。"""
        assert self.gravity is not None
        note = self.gravity.last_note
        if note == self._gravity_warned:
            return
        self._gravity_warned = note
        if note is not None:
            debug_print(self.name, f"重力前馈：{note}", "WARNING")

    @_require_robot
    def set_gripper(self, gripper: float):
        """下发夹爪目标：入参为归一化开合度 [0, 1]（越界裁切，policy 输出无界属常态）。

        ``move_gripper_m`` 取米（行程 0.1 m）→ 归一化值 / 10；与 ``get_gripper`` 的
        ``* 10`` 互为逆变换。
        """
        gripper = float(np.clip(gripper, 0, 1))
        self.gripper.move_gripper_m(gripper / 10)
        debug_print(self.name, f"set gripper to {gripper}", "DEBUG")

    @_require_robot
    def emergency_stop(self):
        """硬件急停（断电 e-stop）：**当前无调用方**（safe_stop 是软停），保留待后续接入。"""
        self.robot.electronic_emergency_stop()


if __name__ == "__main__":
    set_log_level("DEBUG")

    controller = PiperController("robot_controller")
    controller.connect(port="can_left")

    state = controller.get_joint()
    print(f"Initial joint: {state}")

    controller.set_gripper(0.99)
    for i in range(40):
        controller.set_joint(np.array([0.036, 0.046, -0.407, -0.081, 0.471, 0.216]))
        time.sleep(0.05)  # Wait for the commands to take effect

    controller.set_joint(np.array([0.036, 0.046, -0.407, -0.081, 0.471, 0.216]))
    time.sleep(1)  # Wait for the commands to take effect
    state = controller.get_joint()
    print(f"Joint after commands: {state}")
