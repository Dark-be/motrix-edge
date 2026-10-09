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

"""深度观测（robot-pipeline 侧）：相机声明 / 配置解析 / 取帧组装 / 相机元数据。

钉住深度契约（见 ``wiki/design/robot_pipeline_depth.md``）：

- **只有具备深度的相机**（``DEPTH_CAMERAS``，RealSense 类）才可能发布
  ``observations/depth/<cam>``；深度**对齐到彩色图**（与同名彩色帧同一像素网格）；
- 生效集合由 ``robot.depth`` 决定（``enabled`` / ``cameras``），非法相机名 → 启动报错
  （不替现场猜，也不静默丢弃）；
- 深度是**附加**观测：彩色（``observations/images/``）与状态照常，且深度键**不进数据集**
  （采集器只收 ``observations/images/`` 前缀）；
- 相机**静态元数据**（尺寸 / 彩色内参 / 深度比例）由 ``camera_meta()`` 给出，机器人进程经
  ``GET /v1/cameras`` 上报（深度换算成米靠它）。

用**虚拟机器人**（``test_robot``）跑：无硬件、合成深度逐列线性（``depth_mm = 1000 + u``），
故断言可以精确到具体数值。
"""

import importlib
import sys
import types
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"
_QPOS = [0.0] * 12  # joint 空间：每臂 6 关节角 × 2 臂
_GRIPPER = [1.0, 1.0]  # gripper 空间：每臂 1 夹爪 × 2 臂（1 = 张开）


def _stub_hardware_sdks() -> None:
    """硬件 SDK 只装在机器人端：本机离线测试用占位模块（``test_robot`` 用不到，但同目录会引）。"""
    for name in ("pyAgxArm", "pyrealsense2", "v4l2"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)


@pytest.fixture(scope="module")
def test_robot_module():
    """导入 robot-pipeline 的 ``TestRobot``（虚拟机器人，无硬件依赖）。"""
    _stub_hardware_sdks()
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.test_robot")


def _robot(module, depth=None):
    """构造并连接虚拟机器人（连接后深度流才真正打开）。"""
    config: dict = {"init_joint": _QPOS, "init_gripper": _GRIPPER}
    if depth is not None:
        config["depth"] = depth
    robot = module.TestRobot(robot_config=config)
    robot.connect()
    robot.sample_qpos()  # 观测线程的前提：控制线程先出过一拍状态
    return robot


def test_default_depth_cameras_follow_declaration(test_robot_module):
    """缺省开启该机型**所有**具备深度的相机（= ``DEPTH_CAMERAS``，顺序 = ``IMAGE_NAMES`` 子序）。"""
    robot = _robot(test_robot_module)
    assert robot.DEPTH_CAMERAS == ("cam_head",)  # 虚拟相机只有头部合成深度
    assert robot.depth_cameras == ["cam_head"]


def test_observation_carries_depth_aligned_to_color(test_robot_module):
    """观测带 ``observations/depth/<cam>``：uint16、与彩色同尺寸、取值逐列线性（可预测）。"""
    robot = _robot(test_robot_module)
    obs = robot.build_observation()
    assert obs is not None
    depth = obs[f"{robot.DEPTH_PREFIX}cam_head"]
    assert depth.dtype == np.uint16
    assert depth.shape == (480, 640)  # (H, W)，与彩色同网格（已对齐）
    assert int(depth[0, 0]) == 1000
    assert int(depth[-1, 639]) == 1639  # 逐列线性：1000 + u
    # 彩色键照常（深度是附加观测，不替代图像）
    assert f"{robot.CAMERA_PREFIX}cam_head" in obs
    # 无深度的相机不产生深度键
    assert [key for key in obs if key.startswith(robot.DEPTH_PREFIX)] == [f"{robot.DEPTH_PREFIX}cam_head"]


def test_depth_disabled_by_config(test_robot_module):
    """``robot.depth.enabled: false`` → 不开深度流：观测里没有深度键，元数据里也没有深度段。"""
    robot = _robot(test_robot_module, depth={"enabled": False})
    assert robot.depth_cameras == []
    obs = robot.build_observation()
    assert not [key for key in obs if key.startswith(robot.DEPTH_PREFIX)]
    assert all(info["depth"] is None for info in robot.camera_meta())
    assert f"{robot.CAMERA_PREFIX}cam_head" in obs  # 彩色不受影响


def test_depth_cameras_can_be_narrowed(test_robot_module):
    """``robot.depth.cameras`` 可显式列出**具备深度**的相机（子集），顺序跟随 ``IMAGE_NAMES``。"""
    robot = _robot(test_robot_module, depth={"cameras": ["cam_head"]})
    assert robot.depth_cameras == ["cam_head"]


def test_depth_camera_without_depth_capability_is_rejected(test_robot_module):
    """配置里出现**无深度能力**的相机 → 启动报错（不静默忽略、也不替现场猜）。"""
    with pytest.raises(ValueError, match="无深度能力"):
        _robot(test_robot_module, depth={"cameras": ["cam_left_wrist"]})


def test_camera_meta_reports_scale_intrinsics_and_capability(test_robot_module):
    """``camera_meta()``：顺序 = ``IMAGE_NAMES``；深度相机带 ``scale`` 与彩色内参。"""
    robot = _robot(test_robot_module)
    infos = robot.camera_meta()
    assert [info["name"] for info in infos] == list(robot.IMAGE_NAMES)
    meta = {info["name"]: info for info in infos}
    assert meta["cam_head"]["depth"] == {"scale": 0.001, "aligned_to_color": True}
    assert meta["cam_head"]["intrinsics"] == {"fx": 600.0, "fy": 600.0, "cx": 320.0, "cy": 240.0}
    assert (meta["cam_head"]["width"], meta["cam_head"]["height"]) == (640, 480)
    assert meta["cam_left_wrist"]["depth"] is None  # 腕相机无深度（网络摄像头）


def test_camera_meta_declares_mount_and_arm(test_robot_module):
    """``camera_meta()`` 附带**安装方式**（装配事实）：腕相机 → ``wrist`` + 承载臂，其余 ``fixed``。

    这是「相机世界位姿会不会随臂动」的判据：腕相机的外参只能是 ``T_flange_cam``，运行期要乘同拍
    ``FK(q)``（见 ``wiki/design/robot_pipeline_frames.md``）。未标定时它也必须给（与标定产物无关）。
    """
    robot = _robot(test_robot_module)
    meta = {info["name"]: info for info in robot.camera_meta()}
    assert meta["cam_head"]["mount"] == "fixed" and meta["cam_head"]["arm"] is None
    for name in robot.WRIST_CAMERAS:
        assert meta[name]["mount"] == "wrist"
        assert meta[name]["arm"] == robot.WRIST_CAMERAS[name]
    assert robot.camera_mount("cam_head") == ("fixed", None)


def test_wrist_cameras_declared_by_real_machine_types():
    """机型声明：``dual_piper`` 两路腕相机各归其臂；``test_robot``（本替身）无腕相机。

    装配事实写在各机型的类常量里（不是配置、不是标定产物）——读源码文本即可断言，避免在无硬件环境
    导入依赖 SDK 的机型模块。
    """
    import re

    text = (_REPO_ROOT / "robot-pipeline" / "src" / "robot" / "dual_piper_robot.py").read_text(encoding="utf-8")
    assert re.search(r'WRIST_CAMERAS.*cam_left_wrist.*"left".*cam_right_wrist.*"right"', text)
    assert "WRIST_CAMERAS" not in (_REPO_ROOT / "robot-pipeline" / "src" / "robot" / "test_robot.py").read_text(
        encoding="utf-8"
    )
