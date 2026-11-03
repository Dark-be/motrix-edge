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

"""Robot server 自描述的观测键契约。"""

from server.contract_server import _robot_observation_keys


class _Robot:
    IMAGE_NAMES = []

    @staticmethod
    def pose_dim():
        return 6

    @staticmethod
    def depth_camera_names():
        return []


def test_discover_observation_keys_include_pose_when_published():
    assert _robot_observation_keys(_Robot()) == [
        "observations/qpos",
        "observations/qpos_err",
        "action",
        "observations/pose",
        "observations/pose_action",
    ]
