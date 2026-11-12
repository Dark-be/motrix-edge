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

"""Simulation adapter failure-boundary tests."""

import httpx2

from motrix_edge.adapter.simulation_piper_adapter import SimulationPiperAdapter


def test_task_status_returns_unavailable_after_simulation_exits(monkeypatch):
    adapter = SimulationPiperAdapter()

    class DisconnectedClient:
        def get(self, _path):
            raise httpx2.ConnectError("simulation process exited")

    monkeypatch.setattr(adapter, "_client", lambda: DisconnectedClient())

    assert adapter.task_status() is None
