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

"""Launch a configurable scene as an Edge-compatible robot process."""

import argparse
from pathlib import Path

import uvicorn

from .runtime import Settings, Simulation
from .server import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--session", type=Path, required=True, help="Session TOML, relative to workspace")
    parser.add_argument("--scene", type=Path, required=True, help="Scene JSON, relative to workspace")
    parser.add_argument("--output", type=Path, required=True, help="New directory for SDK diagnostics")
    parser.add_argument("--shm-name", default="simulation_piper_obs")
    parser.add_argument("--cameras", nargs=2, default=["cam_head", "cam_right_wrist"])
    parser.add_argument("--no-render", action="store_true", help="Physics test only; no Edge video")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    settings = Settings(
        workspace,
        (workspace / args.session).resolve(),
        (workspace / args.scene).resolve(),
        args.output.resolve(),
        args.shm_name,
        tuple(args.cameras),
        not args.no_render,
    )
    settings.output.mkdir(parents=True, exist_ok=False)
    uvicorn.run(create_app(Simulation(settings)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
