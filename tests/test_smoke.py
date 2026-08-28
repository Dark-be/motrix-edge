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

"""Minimal smoke test —— 验证包可导入且公开 API 存在。

真实单元测试见 test_node / test_policy / test_adapter / test_capture_session /
test_infer_session（均无硬件可跑）。
"""


def test_smoke_imports_package_api():
    import motrix_edge

    assert motrix_edge.EdgeNode is not None
    assert motrix_edge.NodeLifecycle is not None
    assert motrix_edge.NodeState is not None
    assert callable(motrix_edge.get_session)
