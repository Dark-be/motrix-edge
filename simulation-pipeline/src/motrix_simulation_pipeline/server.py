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

"""Robot-pipeline-compatible control surface; no recording endpoints."""

import math
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from motrix_edge.adapter import http_contract as contract
from motrix_edge.adapter.base import CAMERA_PREFIX, KEY_ACTION, KEY_QPOS, KEY_QPOS_ERR

from .runtime import Simulation


class Motion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: list[float]
    layout: str = "joint"
    arms: list[str] | None = None
    command_id: str | None = None


class Reset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str | None = None


def create_app(sim: Simulation) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        sim.start()
        try:
            yield
        finally:
            sim.close()

    app = FastAPI(title="Motrix Simulation Pipeline", lifespan=lifespan)

    @app.get(contract.PATH_HEALTH)
    def health():
        sample = sim.snapshot
        fresh = bool(sample) and sim.motion_snapshot()["sample_age_s"] < 2
        return {
            "ok": sim.healthy() and fresh,
            "detail": sim.error or ("" if fresh else "starting or stalled"),
            "control_hz": 1 / sim.period if sim.period else None,
            "measured_hz": None,
        }

    @app.post(contract.PATH_DISCOVER)
    def discover(request: Request):
        cameras = ["cam_head", "cam_right_wrist"] if sim.settings.render else []
        return {
            "status": "ok",
            "robot": {
                "id": sim.instance_id,
                "name": "simulation_piper",
                "type": "simulation_piper",
                "running": sim.healthy(),
                "supported_adapters": ["simulation_piper"],
                "robot_model_id": "simulated-piper",
                "robot_model_version": "0.1.0",
                "action_dim": 6,
                "action_dims": {"joint": 6, "gripper": 1},
                "state_arms": ["right"],
                "action_layouts": [
                    {
                        "name": name,
                        "per_arm": sum(dim for _, dim in parts),
                        "segments": [{"kind": kind, "dim": dim} for kind, dim in parts],
                    }
                    for name, parts in {
                        "joint": [("joint", 6)],
                        "gripper": [("gripper", 1)],
                        "joint+gripper": [("joint", 6), ("gripper", 1)],
                    }.items()
                ],
                "observation_keys": [KEY_QPOS, KEY_QPOS_ERR, KEY_ACTION] + [CAMERA_PREFIX + n for n in cameras],
                "capabilities": {"capture": False, "execute": True, "streaming": sim.settings.render},
                "endpoint": str(request.base_url).rstrip("/"),
                "shm_name": sim.settings.shm_name,
            },
        }

    def submit(**kwargs):
        try:
            return sim.submit(**kwargs)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post(contract.PATH_EXECUTE)
    @app.post(contract.PATH_ROLLOUT)
    def motion(body: Motion):
        if not all(math.isfinite(v) for v in body.action):
            raise HTTPException(422, "nonfinite action")
        return submit(**body.model_dump())

    @app.post(contract.PATH_RESET)
    def reset(body: Reset = Reset()):
        return submit(operation="reset", command_id=body.command_id)

    @app.post(contract.PATH_SAFE_STOP)
    def safe_stop():
        return submit(operation="stop")

    @app.get("/v1/motion/commands/{command_id}")
    def receipt(command_id: str):
        with sim.lock:
            if command_id not in sim.commands:
                raise HTTPException(404, "unknown or expired command ID")
            return dict(sim.commands[command_id])

    @app.get("/v1/motion/state")
    def state():
        try:
            return sim.motion_snapshot()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get(contract.PATH_CAMERAS)
    def cameras():
        if not sim.initialized.is_set() or sim.error:
            raise HTTPException(503, sim.error or "starting")
        return {"cameras": sim.camera_meta if sim.settings.render else [], "frames": None}

    return app
