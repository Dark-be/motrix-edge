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

"""一对 Piper 的对等双向遥操作：两臂互相跟随对方的实测关节角。"""

import numpy as np
from utils.data_handler import debug_print

from robot.base_robot import BaseRobot
from robot.controller.piper_controller import PiperController


class ForceSinglePiperRobot(BaseRobot):
    """两台 Follower/MIT Piper 组成的对称位置耦合。"""

    NAME = "force_single_piper"
    ADAPTER_TYPE = "force_single_piper"
    ROBOT_MODEL_ID = "force-single-piper"
    ROBOT_MODEL_VERSION = "0.0.0"
    CAPABILITIES = {"capture": True, "execute": True, "streaming": False}

    QPOS = 6
    POSE = 0
    GRIPPER = 1
    ARM_CONTROLLERS = {"arm_1": "slave"}
    IMAGE_NAMES: list[str] = []
    IMAGES: dict[str, tuple[int, int]] = {}
    CAMERA_KINDS: dict[str, str] = {}
    SHM_NAME = "force_single_piper_obs"

    DEFAULT_INIT_JOINT = [0.0] * 6
    DEFAULT_INIT_GRIPPER = [1.0]
    PORT_ROLES = ("master", "slave")
    PORT_KINDS = {role: "can" for role in PORT_ROLES}

    def __init__(self, robot_config: dict | None = None):
        robot_config = dict(robot_config or {})
        robot_config.setdefault("init_joint", self.DEFAULT_INIT_JOINT)
        robot_config.setdefault("init_gripper", self.DEFAULT_INIT_GRIPPER)
        super().__init__(robot_config)
        if self.teleop_follow_space != self.TELEOP_FOLLOW_JOINT:
            raise ValueError("force_single_piper_robot only supports robot.teleop.follow_space='joint'")
        section = robot_config.get("force_feedback") or {}
        if not isinstance(section, dict):
            raise ValueError("robot.force_feedback must be a mapping")
        unknown = sorted(set(section) - {"max_initial_error"})
        if unknown:
            raise ValueError(f"robot.force_feedback contains unknown keys: {unknown}")
        self.max_initial_error = np.asarray(section.get("max_initial_error", [0.25] * 6), dtype=np.float64).reshape(-1)
        if (
            self.max_initial_error.shape[0] != 6
            or not np.all(np.isfinite(self.max_initial_error))
            or np.any(self.max_initial_error <= 0.0)
            or np.any(self.max_initial_error > np.pi)
        ):
            raise ValueError("robot.force_feedback.max_initial_error must contain 6 values within (0, pi]")
        self.ports = self._required_devices("ports", self.PORT_ROLES)
        self.master_firmware = str(robot_config.get("master_firmware", "v188"))
        self.slave_firmware = str(robot_config.get("slave_firmware", "v188"))
        self.controllers = {"master": PiperController("master"), "slave": PiperController("slave")}
        self._master_reset_target: np.ndarray | None = None

    def connect(self):
        """两臂从连接开始即使用完全相同的 Follower/MIT 控制通路。"""
        gravity = self.robot_config.get("gravity")
        self.controllers["master"].connect(
            port=self.ports["master"], role="follower", firmware=self.master_firmware, gravity=gravity
        )
        try:
            self.controllers["slave"].connect(
                port=self.ports["slave"], role="follower", firmware=self.slave_firmware, gravity=gravity
            )
        except Exception:
            self.controllers["master"].disconnect()
            raise
        self.ready = True
        debug_print(self.name, "Force-feedback Piper pair connected", "INFO")

    def disconnect(self):
        self.ready = False
        self._master_reset_target = None
        self.disable_teleop()
        for name, controller in self.controllers.items():
            controller.disconnect()
            debug_print(self.name, f"Disconnect controller {name} done", "INFO")

    @staticmethod
    def _joint_reading(value, side: str) -> np.ndarray:
        joints = np.asarray(value, dtype=np.float64).reshape(-1)
        if joints.shape[0] < 6 or not np.all(np.isfinite(joints[:6])):
            raise RuntimeError(f"{side} joint reading must contain 6 finite values")
        return joints[:6].copy()

    def get_observation_qpos(self) -> np.ndarray:
        value = self.controllers["slave"].get_joint()
        if value is None:
            raise RuntimeError("ForceSinglePiperRobot: slave joint reading unavailable")
        return self._joint_reading(value, "slave")

    def get_observation_gripper(self) -> np.ndarray:
        value = self.controllers["slave"].get_gripper()
        if value is None:
            return np.full(1, np.nan)
        scalar = float(np.asarray(value, dtype=np.float64).reshape(-1)[0])
        return np.asarray([scalar], dtype=np.float64)

    def get_observation_frames(self) -> list:
        return []

    def _apply_action(self, action: np.ndarray):
        values = np.asarray(action, dtype=np.float64).reshape(-1)
        self.controllers["slave"].set_joint(values[:6].copy())
        self.controllers["slave"].set_gripper(float(values[6]))

    def reset(self):
        """主、从臂分别回到映射后的同一零位，且都沿用默认 MIT 参数。"""
        super().reset()
        self._master_reset_target = self.init_joint - self.teleop_joint_offset

    def execute(self, action: np.ndarray, *, layout: str | None = None, arms: list[str] | None = None):
        self._master_reset_target = None
        return super().execute(action, layout=layout, arms=arms)

    def safe_stop(self):
        self._master_reset_target = None
        return super().safe_stop()

    def enable_teleop(
        self,
        mode: str = BaseRobot.TELEOP_MODE_ABSOLUTE,
        follow_axes=None,
        follow_space=None,
    ) -> bool:
        """以同拍实测角接管；随后两侧直接交叉下发，不覆盖默认 MIT 参数。"""
        if mode not in self.TELEOP_MODES:
            return super().enable_teleop(mode, follow_axes, follow_space)
        self.resolve_teleop_follow(follow_space, follow_axes)
        try:
            master_q = self._joint_reading(self.controllers["master"].get_joint(), "master")
            slave_q = self._joint_reading(self.controllers["slave"].get_joint(), "slave")
            master_mapped = master_q + self.teleop_joint_offset
            if mode == self.TELEOP_MODE_ABSOLUTE:
                error = np.abs(master_mapped - slave_q)
                if np.any(error > self.max_initial_error):
                    raise RuntimeError(
                        "force-feedback alignment exceeds max_initial_error on joints "
                        f"{(np.flatnonzero(error > self.max_initial_error) + 1).tolist()}"
                    )
            master_gripper = np.asarray(self.controllers["master"].get_gripper(), dtype=np.float64).reshape(-1)[:1]
            slave_gripper = self.get_observation_gripper()
            if master_gripper.shape[0] != 1 or not np.all(np.isfinite(master_gripper)):
                raise RuntimeError("master gripper reading is not finite")
            # 接管首拍只保持实测位置，避免跳变；不传增益覆盖，沿用默认 MIT 参数。
            self.controllers["master"].set_joint(master_q.copy())
            self.controllers["slave"].set_joint(slave_q.copy())
        except Exception as exc:  # noqa: BLE001 任一侧初始化失败都不启用耦合
            self.last_error = f"force-feedback enable failed: {exc}"
            debug_print(self.name, self.last_error, "ERROR")
            return False

        if not super().enable_teleop(mode, follow_axes, follow_space):
            return False
        self._master_reset_target = None
        self.teleop_master_ref = master_mapped.copy()
        self.teleop_slave_ref = slave_q.copy()
        self.teleop_master_gripper_ref = master_gripper.copy()
        self.teleop_slave_gripper_ref = slave_gripper.copy()
        self.teleop_master_now = master_mapped.copy()
        self.teleop_master_gripper_now = master_gripper.copy()
        self.action = np.concatenate([slave_q, slave_gripper])
        self.target_action = self.action.copy()
        self.last_error = None
        return True

    def step(self):
        if self.teleop_enabled:
            self._step_force_feedback()
            return
        if self._master_reset_target is not None:
            try:
                master_q = self._joint_reading(self.controllers["master"].get_joint(), "master")
                slave_q = self._joint_reading(self.controllers["slave"].get_joint(), "slave")
                master_cmd = self._step_toward(master_q, self._master_reset_target, self.step_rad)
                slave_cmd = self._step_toward(slave_q, self.init_joint, self.step_rad)
                self.controllers["master"].set_joint(master_cmd)
                self.controllers["slave"].set_joint(slave_cmd)
                gripper = float(self.init_gripper[0])
                self.controllers["slave"].set_gripper(gripper)
                self.action = np.concatenate([slave_cmd, [gripper]])
            except Exception as exc:  # noqa: BLE001 双臂复位任一侧失败都停止下发
                self._master_reset_target = None
                self.target_action = None
                self.last_error = f"force-feedback pair reset failed: {exc}"
                debug_print(self.name, self.last_error, "ERROR")
            return
        super().step()

    def _step_force_feedback(self) -> None:
        """把两侧同拍实测角互换为目标，形成参数完全对等的位置耦合。"""
        try:
            master_q = self._joint_reading(self.controllers["master"].get_joint(), "master")
            slave_q = self._joint_reading(self.controllers["slave"].get_joint(), "slave")
            master_mapped = master_q + self.teleop_joint_offset
            master_gripper = np.asarray(self.controllers["master"].get_gripper(), dtype=np.float64).reshape(-1)[:1]
            if master_gripper.shape[0] != 1 or not np.all(np.isfinite(master_gripper)):
                raise RuntimeError("master gripper reading is not finite")

            if self.teleop_mode == self.TELEOP_MODE_DELTA:
                slave_cmd = self.teleop_slave_ref + master_mapped - self.teleop_master_ref
                master_cmd = self.teleop_master_ref + slave_q - self.teleop_slave_ref - self.teleop_joint_offset
                gripper = self.teleop_slave_gripper_ref + master_gripper - self.teleop_master_gripper_ref
            else:
                slave_cmd = master_mapped
                master_cmd = slave_q - self.teleop_joint_offset
                gripper = master_gripper

            gripper_value = float(np.clip(gripper[0], 0.0, 1.0))
            self.controllers["master"].set_joint(master_cmd.copy())
            self.controllers["slave"].set_joint(slave_cmd.copy())
            self.controllers["slave"].set_gripper(gripper_value)
            self.teleop_master_now = master_mapped
            self.teleop_master_gripper_now = master_gripper
            self.action = np.concatenate([slave_cmd, [gripper_value]])
            self.target_action = self.action.copy()
        except Exception as exc:  # noqa: BLE001 双向控制任一侧失败都立即解耦
            message = f"force-feedback decoupled: {exc}"
            self.target_action = None
            self.disable_teleop()
            self.last_error = message
            debug_print(self.name, message, "ERROR")
