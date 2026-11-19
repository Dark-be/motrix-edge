# Confidential Information of Motphys. Not for disclosure or distribution without Motphys's prior
# written consent.
#
# This software contains code, techniques and know-how which is confidential and proprietary to
# Motphys.
#
# Copyright (C) 2020-2026 Motphys Technology Co., Ltd. All Rights Reserved.
#
# This software belongs to the Intellectual Property of Motphys. Use of this software is subject to
# the terms and conditions in the license file accompanying. You may not use this software except
# in compliance with the license file.

from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from motrix_simulation_pipeline.server import create_app
from motrix_simulation_pipeline.task_status import (
    PressCriteria,
    PressSequenceTracker,
    SwitchCriteria,
    SwitchTracker,
)


def _tracker() -> PressSequenceTracker:
    return PressSequenceTracker(
        PressCriteria("table30_press_v1", press_threshold_m=0.0012, release_threshold_m=0.0005, stable_seconds=0.04),
        ("pink", "green", "blue"),
    )


def _sample(tracker: PressSequenceTracker, pink=0.0, green=0.0, blue=0.0, dt=0.02):
    return tracker.update({"pink": pink, "green": green, "blue": blue}, dt)


def test_three_buttons_require_stable_press_release_and_expected_order():
    tracker = _tracker()

    assert _sample(tracker, pink=0.0013)["buttons"]["pink"]["phase"] == "pressing"
    status = _sample(tracker, pink=0.0013)
    assert status["pressed_order"] == ["pink"]
    assert status["buttons"]["pink"]["pressed"] is True
    status = _sample(tracker, pink=0.0004)
    assert status["buttons"]["pink"]["released"] is True

    for color in ("green", "blue"):
        values = {color: 0.0013}
        _sample(tracker, **values)
        _sample(tracker, **values)
        status = _sample(tracker)

    assert status["pressed_order"] == ["pink", "green", "blue"]
    assert status["order_valid"] is True
    assert status["success"] is True


def test_out_of_order_press_is_latched_until_reset():
    tracker = _tracker()

    _sample(tracker, green=0.0013)
    status = _sample(tracker, green=0.0013)
    assert status["pressed_order"] == ["green"]
    assert status["order_valid"] is False
    assert status["success"] is False

    tracker.reset()
    status = _sample(tracker)
    assert status["pressed_order"] == []
    assert status["order_valid"] is True


def test_light_switch_requires_target_angle_low_velocity_and_stability():
    tracker = SwitchTracker(
        SwitchCriteria(
            "turn_on_light_switch_v2",
            angle_tolerance_rad=0.01,
            velocity_tolerance_rad_s=0.02,
            stable_seconds=0.5,
            home_tolerance_rad=0.02,
        )
    )

    assert tracker.update(-0.06, 0.03, -0.066, 0.25)["stable_seconds"] == 0
    assert tracker.update(-0.06, 0.01, -0.066, 0.25)["success"] is False
    status = tracker.update(-0.06, 0.01, -0.066, 0.25)
    assert status["success"] is True
    assert status["progress"][0]["complete"] is True

    tracker.reset()
    status = tracker.update(0.066, 0.0, -0.066, 1.0)
    assert status["status"] == "pending"
    assert status["stable_seconds"] == 0


def test_task_status_endpoint_returns_owner_thread_snapshot():
    sim = MagicMock()
    sim.task_status_snapshot.return_value = {
        "available": True,
        "task": "press_three_buttons",
        "provider": "press_three_buttons_v1",
        "success": False,
        "sample_age_s": 0.01,
    }

    with TestClient(create_app(sim)) as client:
        response = client.get("/v1/task/status")

    assert response.status_code == 200
    assert response.json()["task"] == "press_three_buttons"
    sim.start.assert_called_once_with()
    sim.close.assert_called_once_with()


def test_task_status_endpoint_is_unavailable_before_first_sample():
    sim = MagicMock()
    sim.task_status_snapshot.side_effect = RuntimeError("no task status sample yet")

    with TestClient(create_app(sim)) as client:
        response = client.get("/v1/task/status")

    assert response.status_code == 503
    assert response.json()["detail"] == "no task status sample yet"
