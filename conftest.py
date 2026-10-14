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

"""Edge 与 robot-pipeline 测试共享的 pytest 隔离配置。"""

import os

import pytest

#: 两个项目的「根目录」环境变量（配置 + 日志）：测试统一指到临时目录。
_ROOT_ENV_VARS = ("MOTRIX_EDGE_DIR", "MOTRIX_ROBOT_PIPELINE_DIR")


@pytest.fixture(autouse=True, scope="session")
def _isolate_state_roots(tmp_path_factory):
    """隔离两个项目的运行根，防止测试在仓库工作区播种配置或写日志。"""
    roots = tmp_path_factory.mktemp("roots")
    saved = {name: os.environ.get(name) for name in _ROOT_ENV_VARS}
    for name in _ROOT_ENV_VARS:
        os.environ[name] = str(roots / name.lower())
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
