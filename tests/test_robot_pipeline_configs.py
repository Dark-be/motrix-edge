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

"""robot-pipeline 机型的配置文件：**能被解析** + 关键字段形状正确。

robot-pipeline 自身没有自动化测试，配置只在机器人进程启动时才被 yaml 解析——曾出现过
``collector.skip_until_motion`` 缩进写坏、``test_robot`` 启动直接失败而 CI 全绿的情况。
本用例把「配置可解析 + 关键字段」纳入 CI（配置是 YAML，不是代码，ruff/pytest 都不会碰它）。
"""

import re
from pathlib import Path

import pytest
import yaml

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "robot-pipeline" / "src" / "config"
_CONFIG_FILES = sorted(_CONFIG_DIR.glob("*.yml"))
# 需要「帧头跳过」配置的机型（遥操作系统；其余机型保留代码缺省 HEAD_SKIP_DEFAULTS）
_HEAD_SKIP_CONFIGS = ("test_robot.yml", "dual_piper.yml")


def _load(name: str) -> dict:
    return yaml.safe_load((_CONFIG_DIR / name).read_text())


def test_config_dir_is_populated():
    assert sorted(p.name for p in _CONFIG_FILES), f"未找到 robot-pipeline 机型配置：{_CONFIG_DIR}"


@pytest.mark.parametrize("name", [p.name for p in _CONFIG_FILES])
def test_config_parses_and_declares_required_sections(name):
    data = _load(name)
    assert set(data) >= {"INFO_LEVEL", "server", "robot", "collector"}, f"{name} 缺少顶层段"
    assert data["server"]["host"], f"{name} 的 server.host 为空"
    assert int(data["server"]["port"]) > 0, f"{name} 的 server.port 非法"
    assert data["robot"].get("type"), f"{name} 的 robot.type 为空"


@pytest.mark.parametrize("name", [p.name for p in _CONFIG_FILES])
def test_collector_type_is_known(name):
    assert _load(name)["collector"]["type"] in ("act_mcap", "act_hdf5"), f"{name} 的 collector.type 未知"


@pytest.mark.parametrize("name", _HEAD_SKIP_CONFIGS)
def test_head_skip_block_shape(name):
    """遥操作机型必须显式带 ``collector.skip_until_motion``，且键名/取值范围与代码口径一致。"""
    block = _load(name)["collector"].get("skip_until_motion")
    assert isinstance(block, dict), f"{name} 缺少 collector.skip_until_motion"
    assert set(block) >= {"enabled", "joint_eps", "gripper_eps"}, f"{name} 的 skip_until_motion 键不全"
    assert block["enabled"] is True, f"{name} 的 skip_until_motion.enabled 应为 true"
    assert 0 < float(block["joint_eps"]) < 1, f"{name} 的 joint_eps 应落在 (0, 1) rad"
    assert 0 < float(block["gripper_eps"]) < 1, f"{name} 的 gripper_eps 应落在 (0, 1)"


def _registered_robot_types() -> set[str]:
    """``robot.type`` 的合法取值 = ``robot/__init__.py::ROBOT_REGISTRY`` 的键。

    只读源码文本、不 import（robot-pipeline 的 SDK 依赖不在当前环境里）；解析不到就跳过，
    避免用例因注册表写法变化而误报。
    """
    source = (_CONFIG_DIR.parent / "robot" / "__init__.py").read_text()
    if "ROBOT_REGISTRY" not in source:
        return set()
    block = source.split("ROBOT_REGISTRY", 1)[1].split("}", 1)[0]
    return set(re.findall(r'"([a-z][a-z0-9_]*)"\s*:', block))


@pytest.mark.parametrize("name", [p.name for p in _CONFIG_FILES])
def test_robot_type_is_registered(name):
    registered = _registered_robot_types()
    if not registered:
        pytest.skip("未解析到 ROBOT_REGISTRY")
    assert _load(name)["robot"]["type"] in registered, f"{name} 的 robot.type 不是已注册机型"
