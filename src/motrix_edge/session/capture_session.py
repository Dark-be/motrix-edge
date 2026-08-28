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

import time

from motrix_edge.adapter import AdapterCapability
from motrix_edge.utils.commands import (
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_EPISODE_START,
    CMD_CAPTURE_SYNC,
    CMD_INFER_IP,
    CMD_INFER_IP_SET,
    CMD_INFER_PORT,
    CMD_INFER_PORT_SET,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_RESET,
    CMD_ROBOT_TELEOP,
    CMD_SESSION_QUIT,
    CommandResult,
    ok_result,
    parse_meta,
)
from motrix_edge.utils.data_handler import debug_print

from .base import BaseSession, RunResult, SessionState, _cmd_name


class CaptureSession(BaseSession):
    """数据采集会话 —— 组合 RobotAdapter 的采集执行器（无回合流程控制）。

    生命周期由 EdgeNode 管理（session_start → run → session_finish）：进入会话后
    持续消费命令（session quit 退出、robot estop 急停、robot execute / teleop、capture
    episode / sync）。**显示观测由节点级持续写入 frame_manager**，本会话不再 observe /
    写 frame_manager——采集只负责驱动机器人进程采集（episode 起止、元信息同步）。
    """

    def __init__(self, base_cfg, command_source=None, frame_manager=None, adapter=None):
        super().__init__(
            base_cfg=base_cfg,
            name="CaptureSession",
            command_source=command_source,
            frame_manager=frame_manager,
            adapter=adapter,
        )
        if self.adapter is None:
            raise ValueError("capture session requires an injected adapter (owned by node)")
        if not self.adapter.capabilities.supports(AdapterCapability.CAPTURE):
            raise ValueError("injected adapter does not support CAPTURE capability")

        self.state = SessionState.INIT  # 实时状态（供外部查询）

    def session_start(self):
        """进入会话（节点进入 ACTIVE 前调用）：adapter 已由节点 discover 绑定，直接就绪。"""
        self.state = SessionState.READY

    def session_finish(self):
        """释放资源（节点释放会话时调用）。adapter 由节点持有，不在此释放。"""
        self.state = SessionState.FINISHED

    def run(self):
        """阻塞式任务主循环：持续消费命令，直到退出 / 急停。返回 RunResult。

        观测由节点级持续写入 frame_manager，本会话不 observe / 写 frame_manager。
        """
        # 复位 + 等待就绪（期间可 session quit 退出 / robot estop 急停 / robot reset 复位）
        self.adapter.reset()
        result = self._wait_ready(CMD_SESSION_QUIT)
        if result is not None:
            self.state = SessionState.FINISHED if result == RunResult.FINISHED else SessionState.ERROR
            return result
        self.state = SessionState.READY

        debug_print(self.name, "Robot READY. capture commands, session quit to exit.", "INFO")
        while True:
            if self._stop_requested:  # 外部请求停止（node 失联 ERROR）：立即退出
                self.state = SessionState.ERROR
                return RunResult.ERROR
            cmd = self.command_source()
            name = _cmd_name(cmd)
            if name == CMD_SESSION_QUIT:  # 退出采集会话
                self.state = SessionState.FINISHED
                self._record_exit(cmd)
                debug_print(self.name, "Capture session finished.", "INFO")
                return RunResult.FINISHED
            elif name == CMD_ROBOT_ESTOP:  # 急停
                self.safe_stop()
                self.state = SessionState.ERROR
                self._reply(cmd, CommandResult(status="error", error="estop", status_code=500))
                return RunResult.ERROR
            elif name == CMD_ROBOT_EXECUTE:  # 直接下发 raw 动作（qpos 直接作为参数）
                self._execute_action(cmd)
            elif name == CMD_ROBOT_RESET:  # 复位（任务期间；adapter 可用）
                self.adapter.reset()
                self._reply(cmd, ok_result(state="ready"))
            elif name == CMD_ROBOT_TELEOP:  # 遥操作开关（true/false 直接作为参数）
                self._set_teleop(cmd)
            elif name == CMD_CAPTURE_EPISODE_START:  # 开始一轮采集（episode 开始）
                self.adapter.start_capture()
                self._reply(cmd, ok_result(state="ready", episode="start"))
            elif name == CMD_CAPTURE_EPISODE_END:  # 结束一轮采集（episode 结束）
                self.adapter.end_capture()
                self._reply(cmd, ok_result(state="ready", episode="end"))
            elif name in (  # 配置级命令：任务态也可用（infer ip / infer ip set / infer port / infer port set）
                CMD_INFER_IP,
                CMD_INFER_IP_SET,
                CMD_INFER_PORT,
                CMD_INFER_PORT_SET,
            ):
                self._reply(cmd, self._on_infer_endpoint(cmd))
            elif name == CMD_CAPTURE_SYNC:  # 同步采集元信息（采集员 / 任务名等）到机器人进程
                try:
                    meta = parse_meta(cmd.params.get("meta"))
                except ValueError as exc:
                    self._reply(cmd, CommandResult(status="rejected", error=str(exc), status_code=400))
                    continue
                self.adapter.sync_capture_meta(meta)
                self._reply(cmd, ok_result(state="ready", meta=meta))
            else:  # 未识别命令（当前任务不适用）统一回执，避免 submit 挂起
                if cmd is not None:
                    self._reply(cmd, CommandResult(status="rejected", error=f"{name} not applicable", status_code=409))
                time.sleep(0.02)  # 无命令轻量轮询（避免忙等）
