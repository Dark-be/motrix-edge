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

"""``python -m motrix_edge`` / console script ``motrix-edge`` 的命令行入口。

CLI 相关逻辑（main() 及子命令 run / adapters list / version）集中在此；
`node.py` 只保留 EdgeNode 生命周期核心库，不承载入口逻辑。
"""

import threading

from motrix_edge.utils.adapters import print_adapter_details, print_adapters
from motrix_edge.utils.cli import CliSession
from motrix_edge.utils.commands import CommandBus, build_command_registry
from motrix_edge.utils.version import get_package_version


def _print_version() -> None:
    """打印版本号（motrix-edge version / --version）。"""
    print(f"motrix-edge {get_package_version()}")


def _start_web(app, host: str, port: int):
    """后台线程运行 FastAPI 服务，返回 uvicorn.Server（置 should_exit=True 停止）。

    uvicorn 日志：默认（``MOTRIX_LOG_FILE`` 未设 / =0）**全部丢弃**（不写文件、不占终端）；
    ``MOTRIX_LOG_FILE=1`` 时写 ``logs/uvicorn.log``（RotatingFileHandler，10MB × 5，
    HTTP access 只写文件），与 ``debug_print`` 的 ``logs/log_*.txt`` 分开。
    """
    import os

    import uvicorn

    from motrix_edge.config import LOG_PATH
    from motrix_edge.utils.logging import uvicorn_log_config

    log_dir = LOG_PATH
    # 文件日志**默认关闭**（MOTRIX_LOG_FILE=1 开启，uvicorn.log / access 写文件）；防长期运行塞满磁盘
    file_log_enabled = os.getenv("MOTRIX_LOG_FILE", "0").strip().lower() not in ("0", "false", "no")
    if file_log_enabled:
        os.makedirs(log_dir, exist_ok=True)
    log_config = uvicorn_log_config(
        os.path.join(log_dir, "uvicorn.log"),
        file_enabled=file_log_enabled,
    )
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info", log_config=log_config))
    threading.Thread(target=server.run, name="web", daemon=True).start()
    return server


def _run_node(args) -> None:
    """加载配置并启动 EdgeNode（阻塞式主循环，直到 Ctrl-C）。

    配置来源：``run --config <path>`` 指定 yaml 文件路径；缺省选择性加载——环境变量
    ``MOTRIX_CONFIG_DIR`` 指向的外界配置优先，否则包内默认 ``edge.yml``（只读兜底）。
    node 主线程持续运行 + web 作为 node 的独立线程（接收外部 HTTP 请求并驱动
    node），本地 CLI 按键保留。
    """
    import os

    from motrix_edge.config import config_path, get_config_dir, get_log_dir, get_state_dir, load_config
    from motrix_edge.utils.load_file import load_yaml

    explicit = getattr(args, "config", None)
    if explicit:
        base_cfg = load_yaml(explicit)
        config_source = explicit
    else:
        base_cfg = load_config("edge.yml")
        config_source = config_path("edge.yml") or "packaged default (config/edge.yml)"

    from motrix_edge.lease import build_lease_manager
    from motrix_edge.node import EdgeNode
    from motrix_edge.server import create_app
    from motrix_edge.server.capture import CaptureService
    from motrix_edge.server.command import CommandService
    from motrix_edge.server.infer import InferService
    from motrix_edge.server.preview import PreviewService
    from motrix_edge.server.webrtc import WebRTCService
    from motrix_edge.session import UploadSession
    from motrix_edge.utils.data_handler import debug_print

    # 打印配置来源 + 状态 / 日志目录（区分环境变量 MOTRIX_CONFIG_DIR vs 包内默认；文件日志默认关闭）
    config_dir = get_config_dir()
    file_log_enabled = os.getenv("MOTRIX_LOG_FILE", "0").strip().lower() not in ("0", "false", "no")
    debug_print(
        "EdgeNode",
        f"Loaded config: {config_source}"
        f" | config_dir={config_dir or 'packaged default (read-only)'}"
        f" | state_dir={get_state_dir()}"
        f" | log_dir={get_log_dir()}"
        f" | file_logging={'ON' if file_log_enabled else 'OFF (MOTRIX_LOG_FILE=0)'}",
        "INFO",
    )
    os.environ["INFO_LEVEL"] = base_cfg.get("INFO_LEVEL", "DEBUG")

    server_cfg = base_cfg.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8000)

    debug_print(
        "EdgeNode",
        f"web: http://{host}:{port} （node 的独立线程，接受外部 HTTP 请求）",
        "INFO",
    )

    # 共享命令总线：web / CLI 输入线程 push，EdgeNode 主循环 poll。
    registry = build_command_registry()
    bus = CommandBus()
    cli = CliSession(registry)

    # prompt_toolkit 负责行编辑、历史、补全和并发输出保护。
    threading.Thread(target=cli.run, args=(bus,), name="cli-input", daemon=True).start()

    # node 主线程持续运行；web 是 node 的独立线程（node 接收 web 请求）
    # 单 adapter 包：node 启动后按 adapter.host/port 探测并绑定唯一 adapter，采集 / 推理都基于它
    # Edge 级租约（独立于任务）：受控 HTTP 操作（进入任务 / 命令含 estop）须持有；
    # Console 按 renew_interval 定时续租，超期 ttl 未续租则失效需重新激活
    leases = build_lease_manager(base_cfg)
    node = EdgeNode(base_cfg, command_source=bus, lease_manager=leases)
    captures = CaptureService(node, bus, leases=leases)
    infers = InferService(node, bus, leases=leases)
    commands = CommandService(node, bus, leases=leases)
    webrtc = WebRTCService(node, leases=leases)
    # 观测预览服务（独立于采集 / 推理会话）：直接读 node.frame_manager 观测缓存
    preview_service = PreviewService(node, leases=leases)
    uploads = UploadSession(base_cfg)
    web = _start_web(
        create_app(
            base_cfg,
            node=node,  # /v1/health 读 node 已绑定 adapter，不实时 discover
            captures=captures,
            infers=infers,
            commands=commands,
            lease_manager=leases,
            webrtc=webrtc,
            uploads=uploads,
            preview=preview_service,
        ),
        host,
        port,
    )
    try:
        node.run()  # 主线程：持续运行，直到 Ctrl-C
    finally:
        web.should_exit = True  # 停止 web 线程


def main():
    """CLI 入口。

    子命令：
      run [--config <path>]  启动 EdgeNode（--config 指定配置文件路径；缺省 config/edge.yml）
      adapters list          列出所有已注册的机器人 / 策略适配器
      adapters detail        列出所有已注册机器人适配器的能力详情（静态，不探活）
      version                显示 motrix-edge 版本号

    无子命令时等价 ``run``（缺省加载 config/edge.yml）。
    """
    import argparse

    parser = argparse.ArgumentParser(description="EdgeNode 节点生命周期（CLI 信号驱动）")
    parser.add_argument("--version", action="version", version=f"motrix-edge {get_package_version()}")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="启动 EdgeNode（--config 指定配置文件路径）")
    run_parser.add_argument("--config", default=None, help="配置文件路径（yaml），如 /etc/motrix-edge/edge.yaml")

    adapters_parser = subparsers.add_parser("adapters", help="硬件适配器（机器人 / 策略）管理")
    adapters_sub = adapters_parser.add_subparsers(dest="adapter_action", required=True)
    adapters_sub.add_parser("list", help="列出所有支持的机器人 / 策略适配器")
    adapters_sub.add_parser("detail", help="列出所有已注册机器人适配器的能力详情（静态，不探活）")
    subparsers.add_parser("version", help="显示 motrix-edge 版本号")
    args = parser.parse_args()

    # 一次性子命令：直接打印后退出（无需交互会话）。
    if args.command == "adapters" and args.adapter_action == "list":
        print_adapters()
        return
    if args.command == "adapters" and args.adapter_action == "detail":
        print_adapter_details()
        return
    if args.command == "version":
        _print_version()
        return
    if args.command not in (None, "run"):
        parser.error(f"unknown command: {args.command}")
    _run_node(args)


if __name__ == "__main__":
    main()
