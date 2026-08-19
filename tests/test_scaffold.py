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

"""Scaffold 冒烟测试 —— 脚手架阶段恒真测试。

`pytest`（`testpaths=tests`）在本分支需至少收集到一个用例，避免「no tests collected」
（exit code 5）。chore/2 仅含最小包骨架（`__init__.py`），验证包可导入即可；任务运行时
（feat/6）与 HTTP 控制面（feat/3）的单元测试随后续分支落地。
"""


def test_scaffold_package_importable():
    """脚手架包可导入（单一 src-layout 包）。"""
    import motrix_edge

    assert motrix_edge is not None


def test_always_true():
    """恒真：兜底保证 pytest 至少收集到一个用例。"""
    assert True
