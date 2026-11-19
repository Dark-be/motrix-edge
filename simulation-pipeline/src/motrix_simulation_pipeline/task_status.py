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

"""Extensible task-specific status snapshots sampled by the simulation owner thread."""

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from motrix_gen.api.core.runtime import ArticulatedPartEntity, ObjectEntity, RuntimeData


@dataclass(frozen=True)
class TaskStatusContext:
    workspace: Path
    task_name: str


class TaskStatusProvider(Protocol):
    """Task-owned state extraction; every method runs on the SDK owner thread."""

    provider_name: str
    task_name: str

    def start(self, runtime: RuntimeData) -> None: ...

    def reset(self) -> None: ...

    def snapshot(self, runtime: RuntimeData, dt: float) -> dict[str, object]: ...


ProviderFactory = Callable[[TaskStatusContext], TaskStatusProvider]
_PROVIDERS: dict[str, ProviderFactory] = {}


def register_task_status_provider(task_name: str) -> Callable[[ProviderFactory], ProviderFactory]:
    """Register one factory without coupling the control loop to task names."""

    if not task_name or task_name in _PROVIDERS:
        raise ValueError(f"invalid or duplicate task status provider: {task_name}")

    def decorate(factory: ProviderFactory) -> ProviderFactory:
        _PROVIDERS[task_name] = factory
        return factory

    return decorate


def create_task_status_provider(context: TaskStatusContext) -> TaskStatusProvider | None:
    factory = _PROVIDERS.get(context.task_name)
    return None if factory is None else factory(context)


@dataclass(frozen=True)
class PressCriteria:
    version: str
    press_threshold_m: float
    release_threshold_m: float
    stable_seconds: float

    @classmethod
    def load(cls, path: Path) -> "PressCriteria":
        payload = json.loads(path.read_text(encoding="utf-8"))
        criteria = cls(
            version=payload["version"],
            press_threshold_m=float(payload["press_threshold_m"]),
            release_threshold_m=float(payload["release_threshold_m"]),
            stable_seconds=float(payload["stable_seconds"]),
        )
        values = (criteria.press_threshold_m, criteria.release_threshold_m, criteria.stable_seconds)
        if criteria.version != "table30_press_v1" or not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError(f"invalid press_three_buttons success criteria: {path}")
        if criteria.release_threshold_m >= criteria.press_threshold_m:
            raise ValueError("button release threshold must be below press threshold")
        return criteria


class PressSequenceTracker:
    """Latch stable presses and subsequent releases in first-press order."""

    def __init__(self, criteria: PressCriteria, expected_order: tuple[str, ...]):
        if not expected_order or len(set(expected_order)) != len(expected_order):
            raise ValueError("expected button order must contain distinct entries")
        self.criteria = criteria
        self.expected_order = expected_order
        self.reset()

    def reset(self) -> None:
        self.stable = {color: 0.0 for color in self.expected_order}
        self.pressed = {color: False for color in self.expected_order}
        self.released = {color: False for color in self.expected_order}
        self.maximum = {color: 0.0 for color in self.expected_order}
        self.pressed_order: list[str] = []
        self.order_valid = True

    def update(self, displacements: Mapping[str, float], dt: float) -> dict[str, object]:
        if set(displacements) != set(self.expected_order) or not math.isfinite(dt) or dt < 0:
            raise ValueError("button status sample does not match the configured order")
        for color in self.expected_order:
            displacement = float(displacements[color])
            if not math.isfinite(displacement):
                raise ValueError(f"nonfinite {color} button displacement")
            self.maximum[color] = max(self.maximum[color], displacement)
            if not self.pressed[color]:
                self.stable[color] = self.stable[color] + dt if displacement >= self.criteria.press_threshold_m else 0.0
                if self.stable[color] >= self.criteria.stable_seconds:
                    self.pressed[color] = True
                    self.pressed_order.append(color)
                    prefix = self.expected_order[: len(self.pressed_order)]
                    self.order_valid = self.order_valid and tuple(self.pressed_order) == prefix
            elif not self.released[color] and abs(displacement) <= self.criteria.release_threshold_m:
                self.released[color] = True

        buttons: dict[str, object] = {}
        for color in self.expected_order:
            if self.released[color]:
                phase = "released"
            elif self.pressed[color]:
                phase = "pressed"
            elif self.stable[color] > 0:
                phase = "pressing"
            else:
                phase = "pending"
            buttons[color] = {
                "displacement_m": float(displacements[color]),
                "maximum_displacement_m": self.maximum[color],
                "stable_seconds": self.stable[color],
                "pressed": self.pressed[color],
                "released": self.released[color],
                "phase": phase,
            }
        success = self.order_valid and all(self.pressed.values()) and all(self.released.values())
        if success:
            status = "success"
        elif not self.order_valid:
            status = "failed"
        elif self.pressed_order or any(value > 0 for value in self.stable.values()):
            status = "running"
        else:
            status = "pending"
        return {
            "criteria_version": self.criteria.version,
            "expected_order": list(self.expected_order),
            "pressed_order": list(self.pressed_order),
            "order_valid": self.order_valid,
            "buttons": buttons,
            "progress": [
                {
                    "key": color,
                    "label": color,
                    "state": buttons[color]["phase"],
                    "complete": self.released[color],
                    "metrics": {
                        "displacement_m": buttons[color]["displacement_m"],
                        "maximum_displacement_m": buttons[color]["maximum_displacement_m"],
                    },
                }
                for color in self.expected_order
            ],
            "status": status,
            "success": success,
        }


@dataclass(frozen=True)
class SwitchCriteria:
    version: str
    angle_tolerance_rad: float
    velocity_tolerance_rad_s: float
    stable_seconds: float
    home_tolerance_rad: float

    @classmethod
    def load(cls, path: Path) -> "SwitchCriteria":
        payload = json.loads(path.read_text(encoding="utf-8"))
        criteria = cls(
            version=payload["version"],
            angle_tolerance_rad=float(payload["angle_tolerance_rad"]),
            velocity_tolerance_rad_s=float(payload["velocity_tolerance_rad_s"]),
            stable_seconds=float(payload["stable_seconds"]),
            home_tolerance_rad=float(payload["home_tolerance_rad"]),
        )
        values = (
            criteria.angle_tolerance_rad,
            criteria.velocity_tolerance_rad_s,
            criteria.stable_seconds,
            criteria.home_tolerance_rad,
        )
        if criteria.version != "turn_on_light_switch_v2" or not all(
            math.isfinite(value) and value > 0 for value in values
        ):
            raise ValueError(f"invalid turn_on_light_switch success criteria: {path}")
        return criteria


class SwitchTracker:
    """Require the rocker to remain at its target limit with low velocity."""

    def __init__(self, criteria: SwitchCriteria):
        self.criteria = criteria
        self.reset()

    def reset(self) -> None:
        self.stable_seconds = 0.0

    def update(self, angle: float, velocity: float, target: float, dt: float) -> dict[str, object]:
        values = (angle, velocity, target, dt)
        if not all(math.isfinite(value) for value in values) or dt < 0:
            raise ValueError("switch status sample must contain finite values and nonnegative dt")
        angle_error = abs(angle - target)
        at_target = angle_error <= self.criteria.angle_tolerance_rad
        stationary = abs(velocity) <= self.criteria.velocity_tolerance_rad_s
        self.stable_seconds = self.stable_seconds + dt if at_target and stationary else 0.0
        success = self.stable_seconds >= self.criteria.stable_seconds
        state = "complete" if success else "stabilizing" if self.stable_seconds > 0 else "pending"
        return {
            "criteria_version": self.criteria.version,
            "status": "success" if success else "running" if at_target else "pending",
            "success": success,
            "angle_rad": angle,
            "target_angle_rad": target,
            "angle_error_rad": angle_error,
            "velocity_rad_s": velocity,
            "stable_seconds": self.stable_seconds,
            "progress": [
                {
                    "key": "rocker",
                    "label": "light switch",
                    "state": state,
                    "complete": success,
                    "metrics": {
                        "angle_rad": angle,
                        "target_angle_rad": target,
                        "angle_error_rad": angle_error,
                        "velocity_rad_s": velocity,
                        "stable_seconds": self.stable_seconds,
                    },
                }
            ],
        }


class PressThreeButtonsStatus:
    provider_name = "press_three_buttons_v1"
    task_name = "press_three_buttons"
    expected_order = ("pink", "green", "blue")

    def __init__(self, context: TaskStatusContext):
        criteria_path = context.workspace / "tasks" / context.task_name / "success.json"
        self.tracker = PressSequenceTracker(PressCriteria.load(criteria_path), self.expected_order)
        self.joint_names: dict[str, str] = {}

    def start(self, runtime: RuntimeData) -> None:
        for color in self.expected_order:
            raw_name = f"button_{color}"
            objects = [
                entity
                for entity in runtime.gen.entities()
                if isinstance(entity, ObjectEntity) and entity.raw_object_name == raw_name
            ]
            if len(objects) != 1:
                raise ValueError(f"expected one {raw_name} object, found {len(objects)}")
            parts = [
                entity
                for entity in runtime.gen.entities()
                if isinstance(entity, ArticulatedPartEntity)
                and entity.parent_entity_id == objects[0].entity_id
                and entity.role == "button"
            ]
            if len(parts) != 1:
                raise ValueError(f"expected one button part on {raw_name}, found {len(parts)}")
            self.joint_names[color] = parts[0].joint_name

    def reset(self) -> None:
        self.tracker.reset()

    def snapshot(self, runtime: RuntimeData, dt: float) -> dict[str, object]:
        displacements = {
            color: -float(runtime.sim.model.get_joint(joint_name).get_dof_pos(runtime.sim.data)[0])
            for color, joint_name in self.joint_names.items()
        }
        return self.tracker.update(displacements, dt)


@register_task_status_provider("press_three_buttons")
def _press_three_buttons(context: TaskStatusContext) -> TaskStatusProvider:
    return PressThreeButtonsStatus(context)


class TurnOnLightSwitchStatus:
    provider_name = "turn_on_light_switch_v2"
    task_name = "turn_on_light_switch"

    def __init__(self, context: TaskStatusContext):
        criteria_path = context.workspace / "tasks" / context.task_name / "success.json"
        self.tracker = SwitchTracker(SwitchCriteria.load(criteria_path))
        self.joint_name = ""
        self.target_angle = 0.0

    def start(self, runtime: RuntimeData) -> None:
        objects = [
            entity
            for entity in runtime.gen.entities()
            if isinstance(entity, ObjectEntity) and entity.raw_object_name == "socket"
        ]
        if len(objects) != 1:
            raise ValueError(f"expected one socket object, found {len(objects)}")
        prefix = objects[0].root_body_name.removesuffix("socket")
        self.joint_name = prefix + "JNT_socket_switch"
        joint = runtime.sim.model.get_joint(self.joint_name)
        if joint is None:
            raise ValueError("socket rocker joint is missing")
        limits = np.asarray(joint.range, dtype=float).reshape(-1)
        if limits.shape != (2,) or not np.all(np.isfinite(limits)):
            raise ValueError("socket rocker joint must have two finite limits")
        self.target_angle = float(limits[0])

    def reset(self) -> None:
        self.tracker.reset()

    def snapshot(self, runtime: RuntimeData, dt: float) -> dict[str, object]:
        joint = runtime.sim.model.get_joint(self.joint_name)
        if joint is None:
            raise ValueError("socket rocker joint disappeared")
        angle = float(joint.get_dof_pos(runtime.sim.data)[0])
        velocity = float(joint.get_dof_vel(runtime.sim.data)[0])
        return self.tracker.update(angle, velocity, self.target_angle, dt)


@register_task_status_provider("turn_on_light_switch")
def _turn_on_light_switch(context: TaskStatusContext) -> TaskStatusProvider:
    return TurnOnLightSwitchStatus(context)
