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

"""Single-owner SDK execution and detached HTTP/SHM state."""

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import uuid4

import motrixsim as mx
import numpy as np
from motrix_gen.api.config import RenderMode, load_config
from motrix_gen.api.core.runtime import RobotEntity, RuntimeData
from motrix_gen.api.extensions import PrimitiveSkill, register_skill
from motrix_gen.api.session import MotrixSession
from motrix_gen.api.tasks import TaskRequest
from msgspec.structs import replace

from motrix_edge.adapter.base import KEY_ACTION, KEY_QPOS, KEY_QPOS_ERR
from motrix_edge.adapter.shm_contract import ObsShmWriter


@dataclass(frozen=True)
class Settings:
    workspace: Path
    session: Path
    scene: Path
    output: Path
    shm_name: str = "simulation_piper_obs"
    cameras: tuple[str, str] = ("cam_head", "cam_right_wrist")
    render: bool = True
    observation_hz: float = 10.0
    joint_speed: float = 0.7

    def __post_init__(self):
        if not self.workspace.is_dir() or not self.session.is_file() or not self.scene.is_file():
            raise ValueError("workspace, session and scene must exist")
        if len(self.cameras) != 2 or len(set(self.cameras)) != 2:
            raise ValueError("two distinct scene camera names required")
        if not 0 < self.observation_hz <= 30 or not np.isfinite(self.joint_speed) or self.joint_speed <= 0:
            raise ValueError("observation_hz must be in (0,30]; joint_speed must be positive and finite")


class Simulation:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = Lock()
        self.stop = Event()
        self.initialized = Event()
        self.thread = Thread(target=self.run, name="simulation-owner", daemon=False)
        self.error = ""
        self.instance_id = uuid4().hex
        self.commands: OrderedDict[str, dict] = OrderedDict()
        self.pending: list[str] = []
        self.target = np.zeros(7)
        self.home = np.zeros(7)
        self.limits = np.zeros((6, 2))
        self.snapshot: dict = {}
        self.revision = 0
        self.seq = 0
        self.period = 0.0
        self.camera_meta: list[dict] = []
        self.renderer: mx.render.RenderApp | None = None
        self.writer: ObsShmWriter | None = None

    def start(self):
        if ExternalControl.simulation is not None:
            raise RuntimeError("only one simulation per process is supported")
        ExternalControl.simulation = self
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(30)
        if self.thread.is_alive():
            raise RuntimeError("simulation owner did not stop within 30 seconds")
        ExternalControl.simulation = None

    def healthy(self) -> bool:
        return (
            self.initialized.is_set()
            and not self.error
            and not self.stop.is_set()
            and self.thread.is_alive()
            and bool(self.snapshot)
        )

    def submit(self, action=None, layout="joint", arms=None, command_id=None, operation="move") -> dict:
        if arms is not None and arms != ["right"]:
            raise ValueError("arms must be ['right']")
        layouts = {"joint": 6, "gripper": 1, "joint+gripper": 7}
        if layout not in layouts:
            raise ValueError("supported layouts: joint, gripper, joint+gripper")
        values = np.asarray(action if action is not None else [], dtype=float)
        if operation == "move" and (values.shape != (layouts[layout],) or not np.all(np.isfinite(values))):
            raise ValueError("invalid action dimensions or nonfinite values")
        if operation == "move":
            if layout != "gripper" and np.any((values[:6] < self.limits[:, 0]) | (values[:6] > self.limits[:, 1])):
                raise ValueError("joint target outside model limits")
            if "gripper" in layout and not 0 <= values[-1] <= 1:
                raise ValueError("gripper must be in [0,1]")
        if command_id is not None and (not isinstance(command_id, str) or not 0 < len(command_id) <= 128):
            raise ValueError("command_id must be a nonempty string of at most 128 characters")
        key = command_id or uuid4().hex
        request = {"operation": operation, "layout": layout, "action": values.tolist()}
        with self.lock:
            if not self.healthy():
                raise RuntimeError(self.error or "simulation not ready")
            if key in self.commands:
                prior = self.commands[key]
                if prior["request"] != request:
                    raise ValueError("command_id reused with a different request")
                return dict(prior)
            if operation == "stop":
                for pending in self.pending:
                    self.commands[pending].update(execution_status="rejected", error="superseded by safe stop")
                self.pending.clear()
            if len(self.commands) >= 256:
                removable = next((k for k, v in self.commands.items() if v["execution_status"] != "queued"), None)
                if removable is None:
                    raise RuntimeError("command queue full")
                del self.commands[removable]
            receipt = {
                "status": "accepted",
                "command_id": key,
                "instance_id": self.instance_id,
                "execution_status": "queued",
                "motion_revision": self.revision,
                "request": request,
            }
            self.commands[key] = receipt
            self.pending.append(key)
            return dict(receipt)

    def motion_snapshot(self):
        with self.lock:
            if not self.snapshot:
                raise RuntimeError("no motion sample yet")
            return {**self.snapshot, "sample_age_s": max(0, time.monotonic() - self.snapshot["sample_monotonic"])}

    def run(self):
        try:
            config = load_config(self.settings.session)
            config = replace(
                config,
                workspace_root=self.settings.workspace,
                config_path=None,
                scene=replace(config.scene, path=self.settings.scene),
                render=replace(config.render, mode=RenderMode.NONE),
                record=replace(config.record, enabled=False),
                agent=replace(config.agent, artifact_root=self.settings.output / "artifacts"),
            )
            with MotrixSession() as session:
                session.start(config)
                result = session.run_task(
                    TaskRequest(
                        str(uuid4()),
                        "external robot control",
                        mode="skills",
                        input_json=json.dumps({"skills": [{"skill_type": "ExternalControl", "params": {}}]}),
                    )
                )
                if not self.stop.is_set():
                    raise RuntimeError(f"control task ended unexpectedly: {result.outcome}: {result.result}")
        except Exception as exc:
            self.error = str(exc)
        finally:
            with self.lock:
                for receipt in self.commands.values():
                    if receipt["execution_status"] == "queued":
                        receipt.update(execution_status="failed", error=self.error or "service stopped")
            if self.renderer is not None:
                self.renderer.close()
            if self.writer is not None:
                self.writer.set_flags(running=False)
                self.writer.close()
                self.writer.unlink()
            self.initialized.set()


@register_skill
class ExternalControl(PrimitiveSkill):
    """External commands become actuator outputs; the SDK owns physical stepping."""

    simulation: Simulation | None = None

    def __init__(self):
        super().__init__("external-control", max_time=float("inf"))
        self.deadline = time.monotonic()
        self.next_frame = 0.0
        self.captures: list[mx.render.CaptureTask] = []
        self.frame_state: tuple[np.ndarray, np.ndarray] | None = None
        self.capture_started = 0.0

    def start(self, runtime: RuntimeData):
        super().start(runtime)
        sim = self.simulation
        if sim is None:
            raise RuntimeError("simulation not bound")
        robots = [e for e in runtime.gen.entities() if isinstance(e, RobotEntity)]
        if len(robots) != 1:
            raise ValueError("scene must contain exactly one robot")
        self.robot = robots[0].robot_info.manipulator()
        robot = self.robot
        if (
            robot.chain is None
            or len(robot.arm_actuator_indices) != 6
            or robot.jaw_joint is None
            or robot.gripper_actuator_index is None
        ):
            raise ValueError("requires one six-joint arm and one jaw gripper")
        if robot.gripper_open_value == robot.gripper_close_value:
            raise ValueError("gripper range must be nonzero")
        for i, idx in enumerate(robot.arm_actuator_indices):
            actuator = runtime.sim.model.actuators[idx]
            joint = runtime.sim.model.get_joint(actuator.target_name)
            sim.limits[i] = joint.range
        sim.home = self.read_state(runtime)
        sim.home[:6] = np.clip(
            runtime.sim.data.actuator_ctrls[list(robot.arm_actuator_indices)], sim.limits[:, 0], sim.limits[:, 1]
        )
        sim.target = sim.home.copy()
        self.command = sim.home.copy()
        sim.period = self.ctrl_dt
        self.deadline = time.monotonic()
        self.camera_indices = []
        for alias, name in zip(("cam_head", "cam_right_wrist"), sim.settings.cameras, strict=True):
            matches = [c for c in runtime.sim.model.cameras if c.name == name]
            if not matches:
                matches = [c for c in runtime.sim.model.cameras if c.name.endswith(name)]
            if len(matches) != 1:
                raise ValueError(f"scene camera missing or ambiguous: {name}")
            camera = matches[0]
            camera.set_render_target("image", 640, 480)
            self.camera_indices.append(int(camera.index))
            focal = 240 / np.tan(float(camera.fovy) * np.pi / 360)
            sim.camera_meta.append(
                {
                    "name": alias,
                    "width": 640,
                    "height": 480,
                    "intrinsics": {"fx": focal, "fy": focal, "cx": 320, "cy": 240},
                    "depth": None,
                    "mount": "fixed" if alias == "cam_head" else "wrist",
                    "arm": None if alias == "cam_head" else "right",
                }
            )
        sim.writer = ObsShmWriter(sim.settings.shm_name, 2 if sim.settings.render else 0, (640, 480), 7, 7, 7)
        if sim.settings.render:
            sim.renderer = mx.render.RenderApp(headless=True)
            sim.renderer.launch(runtime.sim.model)
        sim.writer.set_flags(running=True, capturing=False)
        sim.initialized.set()

    def read_state(self, runtime: RuntimeData) -> np.ndarray:
        robot = self.robot
        width = float(robot.jaw_joint.get_dof_pos(runtime.sim.data)[0])
        opening = (width - robot.gripper_close_value) / (robot.gripper_open_value - robot.gripper_close_value)
        return np.r_[robot.chain.get_dof_pos(runtime.sim.data), np.clip(opening, 0, 1)]

    def execute(self, runtime: RuntimeData) -> np.ndarray:
        sim = self.simulation
        self.deadline += self.ctrl_dt
        sim.stop.wait(max(0, self.deadline - time.monotonic()))
        if time.monotonic() - self.deadline > self.ctrl_dt:
            self.deadline = time.monotonic()
        actual = self.read_state(runtime)
        with sim.lock:
            for key in sim.pending:
                receipt = sim.commands[key]
                request = receipt["request"]
                if request["operation"] == "reset":
                    sim.target = sim.home.copy()
                elif request["operation"] == "stop":
                    sim.target = actual.copy()
                    self.command = actual.copy()
                elif request["layout"] == "joint":
                    sim.target[:6] = request["action"]
                elif request["layout"] == "gripper":
                    sim.target[6] = request["action"][0]
                else:
                    sim.target[:] = request["action"]
                sim.revision += 1
                receipt.update(execution_status="applied", motion_revision=sim.revision)
            sim.pending.clear()
            sim.seq += 1
            target = sim.target.copy()
            sim.snapshot = {
                "instance_id": sim.instance_id,
                "motion_revision": sim.revision,
                "sample_seq": sim.seq,
                "sample_monotonic": time.monotonic(),
                "teleop": False,
                "observation": {
                    KEY_QPOS: actual.tolist(),
                    KEY_ACTION: target.tolist(),
                    KEY_QPOS_ERR: (target - actual).tolist(),
                },
            }
        now = time.monotonic()
        if sim.renderer is not None:
            if self.captures:
                sim.renderer.sync(None, wait=False)
                if all(task.state == "done" for task in self.captures):
                    images = [np.array(task.take_image().pixels[:, :, :3], copy=True) for task in self.captures]
                    state, frame_target = self.frame_state
                    sim.writer.write(state, frame_target - state, frame_target, images)
                    self.captures = []
                elif now - self.capture_started > 10:
                    raise RuntimeError("renderer frame readback timed out")
            if not self.captures and now >= self.next_frame:
                self.captures = [sim.renderer.get_camera(i).capture() for i in self.camera_indices]
                self.frame_state = (actual.copy(), target.copy())
                sim.renderer.sync(runtime.sim.data, wait=False)
                self.capture_started = now
                self.next_frame = now + 1 / sim.settings.observation_hz
        elif now >= self.next_frame:
            sim.writer.write(actual, target - actual, target, [])
            self.next_frame = now + 1 / sim.settings.observation_hz
        self.command[:6] += np.clip(
            target[:6] - self.command[:6],
            -sim.settings.joint_speed * self.ctrl_dt,
            sim.settings.joint_speed * self.ctrl_dt,
        )
        self.command[6] = target[6]
        action = np.array(runtime.sim.data.actuator_ctrls, copy=True)
        action[list(self.robot.arm_actuator_indices)] = self.command[:6]
        action[self.robot.gripper_actuator_index] = self.robot.gripper_close_value + self.command[6] * (
            self.robot.gripper_open_value - self.robot.gripper_close_value
        )
        return action

    def _check_completion(self, runtime: RuntimeData) -> bool:
        return self.simulation.stop.is_set()
