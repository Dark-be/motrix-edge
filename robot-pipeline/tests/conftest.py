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

"""robot-pipeline 独立测试运行时的状态目录隔离。"""

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_robot_pipeline_root(tmp_path_factory):
    """独立运行本目录测试时，禁止配置与日志写入源码工作区。"""
    name = "MOTRIX_ROBOT_PIPELINE_DIR"
    previous = os.environ.get(name)
    os.environ[name] = str(tmp_path_factory.mktemp("robot-pipeline-root"))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
