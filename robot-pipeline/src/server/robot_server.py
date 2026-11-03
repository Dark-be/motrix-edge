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
- 配置文件名：``--config <name>`` 或环境变量 ``MOTRIX_ROBOT_PIPELINE_CFG``
  （默认 ``test_robot.yml``，虚拟机器人、无硬件，最安全）。
- 机器人：由配置 ``robot.type`` 经 robot 注册表懒加载实例化（``get_robot``）。
- 运行环境：由 ``get_env(cfg)`` 自动构造（BaseEnv + 对应机器人 + 采集配置）。
- HTTP / 共享内存契约：``server.contract_server``（create_app / serve）。
- 启动清单：``build_app`` 打一段 ``config`` / ``machine`` / ``robot`` / ``log`` / ``collect``
  （现场排障先看这几行；INFO 及以上级别，``INFO_LEVEL: ERROR`` 时整段不打）。

新增机器人只需在 ``ROBOT_REGISTRY`` 注册 + 写一个 yml 配置，无需新增 server/env 文件。

启动方式（二选一）:
  1) MOTRIX_ROBOT_PIPELINE_CFG=test_robot.yml uv run uvicorn server.robot_server:app --host 0.0.0.0 --port 8090
  2) uv run python src/server/robot_server.py --config test_robot.yml --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from config import (  # noqa: E402
    ENV_CONFIG_NAME,
    ENV_MACHINE,
    effective_config_path,
    load_config,
    machine_path,
    resolve_machine,
)
from env import get_env  # noqa: E402
from robot import ROBOT_REGISTRY  # noqa: E402
from server.contract_server import create_app, serve  # noqa: E402
from utils.data_handler import debug_print, set_log_level  # noqa: E402

_DEFAULT_CFG = "test_robot.yml"
_LABEL_WIDTH = 9  # 启动清单的标签列宽（最长的 ``collect`` 7 字符 + 2 空格）


def _machine_note(machine: str | None) -> str:
    """启动清单的机器档案行：``<名>（档案 <路径>）`` / ``<名>（无档案 → 按机型默认）`` / ``-``。"""
    if not machine:
        return "-"
    path = machine_path(machine)
    return f"{machine}（档案 {path}）" if path.exists() else f"{machine}（无档案 → 按机型默认）"


def _robot_note(robot_cfg: dict) -> str:
    """启动清单的机器人行：``<type>（<类名>）`` + 配置里出现的接线 / 前馈摘要（没有的项不写）。"""
    robot_type = robot_cfg.get("type") or "-"
    class_name = (ROBOT_REGISTRY.get(robot_type) or (None, None))[1]
    bits = [f"{robot_type}（{class_name}）" if class_name else robot_type]
    ports = robot_cfg.get("ports") or {}
    cameras = robot_cfg.get("cameras") or {}
    if ports:
        bits.append(f"ports {len(ports)} 路")
    if cameras:
        bits.append(f"cameras {len(cameras)} 路")
    gravity = robot_cfg.get("gravity") or {}
    if gravity.get("enabled"):
        bits.append(f"gravity α={gravity.get('alpha')} / t_ff_limit={gravity.get('t_ff_limit')} N·m")
    return " | ".join(bits)


def _collector_note(collector_cfg: dict | None) -> str:
    """启动清单的采集行：落盘目录（**绝对路径**，相对路径按启动时 cwd 解析）+ 编码 + 帧头跳过。"""
    cfg = collector_cfg or {}
    save_dir = Path(cfg.get("save_dir", "./data")).expanduser().resolve()
    bits = [f"save_dir={save_dir}", f"image_format={cfg.get('image_format', 'jpeg')}"]
    skip = (cfg.get("skip_until_motion") or {}).get("enabled")
    if skip is not None:
        bits.append(f"skip_until_motion={'on' if skip else 'off'}")
    return " | ".join(bits)


def _log_startup(name: str, machine: str | None, cfg: dict, level: str) -> None:
    """启动清单：一次把**实际生效**的上下文列全（现场排障先看这几行）。

    ``config`` 行给实际读到的文件（本地副本 / 包内示例），``machine`` 行给档案实际路径——
    现场「改了包内示例不生效」「以为加载了档案」这两个问题看这两行就能判断。
    """
    debug_print("SERVER", "robot process server 启动", "INFO")
    path = effective_config_path(name)
    for label, value in (
        ("config", f"{path.name} → {path}"),
        ("machine", _machine_note(resolve_machine(machine))),
        ("robot", _robot_note(cfg.get("robot") or {})),
        ("log", f"{level}（yml 的 INFO_LEVEL；不写 → 代码缺省 INFO）"),
        ("collect", _collector_note(cfg.get("collector"))),
    ):
        debug_print("SERVER", f"  {label:<{_LABEL_WIDTH}} {value}", "INFO")


def build_app(config_name: str | None = None, machine: str | None = None):
    """按机器人控制配置构造运行环境并构建 /v1 契约应用（服务器本身不读配置）。

    ``config_name`` = 机型配置名（缺省 ``$MOTRIX_ROBOT_PIPELINE_CFG``，仍缺省则 ``test_robot.yml``）。
    ``machine`` = 机器档案名（``<根>/config/robot/<machine>.yml``，``<根>`` = ``$MOTRIX_ROBOT_PIPELINE_DIR``
    或 ``<cwd>/motrix-robot-pipeline``）：只写「这台机器
    不同」的键（``ports`` / ``cameras`` / ``name`` / ``save_dir`` / ``gravity.arms`` / ``can.bindings``），
    深合并到机型配置上；缺省按 ``MOTRIX_ROBOT_PIPELINE_MACHINE`` / hostname 解析（生成档案见
    ``scripts/setup_robot.sh``）。

    监听地址取自配置 ``server`` 段（host/port），存入 ``app.state`` 供启动时读取；
    discover 上报的 endpoint 也使用该地址。
    """
    name = config_name or os.getenv(ENV_CONFIG_NAME) or _DEFAULT_CFG
    cfg = load_config(name, machine)
    # 日志级别与 edge 同一套语义：环境变量 > yml 的 INFO_LEVEL > INFO（详见 utils.data_handler）
    _log_startup(name, machine, cfg, set_log_level(cfg.get("INFO_LEVEL")))
    server_cfg = cfg.get("server") or {}
    host = server_cfg.get("host")
    port = server_cfg.get("port")
    app = create_app(get_env(cfg), host=host, port=port)
    app.state.host = host
    app.state.port = port
    return app


# uvicorn CLI 入口：`uvicorn server.robot_server:app`（也可 `--factory server.robot_server:build_app`）。
# 惰性构建（PEP 562）：uvicorn 取属性时才建一份并缓存回模块命名空间；直接跑本文件（方式二）也只建一次——
# 若在此处直接 `app = build_app()`，方式二会「先建一份再丢弃、再用 --config 重建第二份」：白读一遍配置、
# 日志打两遍，且丢弃的那份已经建过 robot / collector（将来构造函数里接硬件就会变成双开）。
def __getattr__(name: str):
    if name == "app":
        app_obj = build_app()
        globals()["app"] = app_obj
        return app_obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main():
    parser = argparse.ArgumentParser(
        description="Robot process server (config-driven, auto-matches robot by robot.type)"
    )
    parser.add_argument(
        "--config", default=None, help=f"config file name under config; default: ${ENV_CONFIG_NAME} or test_robot"
    )
    parser.add_argument(
        "--machine",
        default=None,
        help="machine profile name under <root>/config/robot ($MOTRIX_ROBOT_PIPELINE_DIR); "
        f"default: ${ENV_MACHINE} or hostname",
    )
    parser.add_argument("--host", default=None, help="override host (default: config server.host or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="override port (default: config server.port or 8090)")
    args = parser.parse_args()
    # 只建一次：不再复用导入期构建的 app（惰性构建见 __getattr__，导入本模块不再有副作用）
    app_obj = build_app(args.config, args.machine)
    host = args.host or getattr(app_obj.state, "host", None)
    port = args.port or getattr(app_obj.state, "port", None)
    serve(app_obj, host=host, port=port)


if __name__ == "__main__":
    main()
