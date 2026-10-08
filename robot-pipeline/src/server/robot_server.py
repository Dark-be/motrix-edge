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

"""通用机器人进程服务器入口（**按 config 自动匹配机器人**）。

单一入口取代 per-robot 的 xxx_server.py，全部由配置驱动：
- 配置文件名：``--config <name>`` 或环境变量 ``ROBOT_SERVER_CFG``
  （默认 ``test_robot.yml``，虚拟机器人、无硬件，最安全）。
- 机器人：由配置 ``robot.type`` 经 robot 注册表懒加载实例化（``get_robot``）。
- 运行环境：由 ``get_env(cfg)`` 自动构造（BaseEnv + 对应机器人 + 采集配置）。
- HTTP / 共享内存契约：``server.contract_server``（create_app / serve）。

新增机器人只需在 ``ROBOT_REGISTRY`` 注册 + 写一个 yml 配置，无需新增 server/env 文件。

启动方式（二选一）:
  1) ROBOT_SERVER_CFG=test_robot.yml uv run uvicorn server.robot_server:app --host 0.0.0.0 --port 8090
  2) uv run python src/server/robot_server.py --config test_robot.yml --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import argparse

from config import load_config, machine_path, resolve_machine  # noqa: E402
from env import get_env  # noqa: E402
from server.contract_server import create_app, serve  # noqa: E402
from utils.data_handler import debug_print, set_log_level  # noqa: E402

_DEFAULT_CFG = "test_robot.yml"


def _machine_note(machine: str | None) -> str:
    """启动日志里的机器档案说明：``<名>（档案 <路径>）`` / ``<名>（无档案 → 按机型默认）`` / ``-``。"""
    if not machine:
        return "-"
    path = machine_path(machine)
    return f"{machine}（档案 {path}）" if path.exists() else f"{machine}（无档案 → 按机型默认）"


def build_app(config_name: str | None = None, machine: str | None = None):
    """按机器人控制配置构造运行环境并构建 /v1 契约应用（服务器本身不读配置）。

    ``machine`` = 机器档案名（``<根>/config/robot/<machine>.yml``，``<根>`` = ``$MOTRIX_ROBOT_PIPELINE_DIR``
    或 ``<cwd>/motrix-robot-pipeline``）：只写「这台机器
    不同」的键（``ports`` / ``cameras`` / ``name`` / ``save_dir`` / ``gravity.arms`` / ``can.bindings``），
    深合并到机型配置上；缺省按 ``MOTRIX_ROBOT_PIPELINE_MACHINE`` / hostname 解析（生成档案见
    ``scripts/setup_robot.sh``）。

    监听地址取自配置 ``server`` 段（host/port），存入 ``app.state`` 供启动时读取；
    discover 上报的 endpoint 也使用该地址。
    """
    name = config_name or _DEFAULT_CFG
    cfg = load_config(name, machine)
    # 日志级别与 edge 同一套语义：环境变量 > yml 的 INFO_LEVEL > INFO（详见 utils.data_handler）
    debug_print(
        "SERVER",
        f"config={name} | machine={_machine_note(resolve_machine(machine))} | "
        f"log_level={set_log_level(cfg.get('INFO_LEVEL'))}",
        "INFO",
    )
    server_cfg = cfg.get("server") or {}
    host = server_cfg.get("host")
    port = server_cfg.get("port")
    app = create_app(get_env(cfg), host=host, port=port)
    app.state.host = host
    app.state.port = port
    return app


# uvicorn CLI 入口：`ROBOT_SERVER_CFG=... uv run uvicorn server.robot_server:app`
app = build_app()


def main():
    parser = argparse.ArgumentParser(
        description="Robot process server (config-driven, auto-matches robot by robot.type)"
    )
    parser.add_argument(
        "--config", default=None, help="config file name under config; default: $ROBOT_SERVER_CFG or test_robot"
    )
    parser.add_argument(
        "--machine",
        default=None,
        help="machine profile name under <root>/config/robot ($MOTRIX_ROBOT_PIPELINE_DIR); "
        "default: $MOTRIX_ROBOT_PIPELINE_MACHINE or hostname",
    )
    parser.add_argument("--host", default=None, help="override host (default: config server.host or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="override port (default: config server.port or 8090)")
    args = parser.parse_args()
    app_obj = build_app(args.config, args.machine) if (args.config or args.machine) else app
    host = args.host or getattr(app_obj.state, "host", None)
    port = args.port or getattr(app_obj.state, "port", None)
    serve(app_obj, host=host, port=port)


if __name__ == "__main__":
    main()
