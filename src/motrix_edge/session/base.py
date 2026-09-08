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

"""session 基类 —— 会话（任务执行器）的最小接口，不包含节点生命周期。

节点生命周期（IDLE / ACTIVE / ERROR）由 EdgeNode（见 node.py）统一管理。
会话仅作为被节点选择性实例化、启停的任务执行器：
  session_start()  节点进入 ACTIVE 前调用：连接硬件、初始化会话
  run()            节点进入 ACTIVE 时调用：阻塞式会话执行，返回 RunResult 告知结束原因
  session_finish() 节点释放会话资源时调用：断开硬件
  safe_stop()      安全停止（幂等、失败安全）：急停/异常时立即停止机器人运动
"""

import time

from motrix_edge.frame import FrameManager
from motrix_edge.utils.commands import (
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_RESET,
    CommandResult,
    handle_capture_meta,
    handle_infer_endpoint,
    ok_result,
    parse_bool,
    parse_qpos,
)
from motrix_edge.utils.data_handler import debug_print


def _noop_command_source():
    """默认命令源：无输入（返回 None）。实际 CLI / HTTP 经 CommandBus 注入。"""
    return None


def _cmd_name(src) -> str | None:
    """命令源返回值 → 命令名（兼容 Command / 命令名字符串 / None）。"""
    if src is None:
        return None
    return getattr(src, "name", None) or (src if isinstance(src, str) else None)


class RunResult:
    """会话 run() 的返回结果 —— session → node 的任务结束契约。

    OK          任务执行成功（会话继续运行）
    FINISHED    任务正常结束（节点释放会话并回到 IDLE，等待下一次任务选择）
    ERROR       硬件/通信异常（已安全停止；节点转 ERROR）
    INTERRUPTED 当前回合被打断（已丢弃未提交数据）、会话仍可继续（会话回 READY）
    """

    OK = "ok"
    FINISHED = "finished"
    ERROR = "error"
    INTERRUPTED = "interrupted"


class SessionState:
    """会话实时状态（供外部随时查询当前任务流程所处阶段）。"""

    INIT = "init"  # 已创建，未连接
    READY = "ready"  # 运行中（持续观测 / 持续推理）
    ERROR = "error"  # 硬件/通信异常
    FINISHED = "finished"  # 会话结束


class BaseSession:
    """所有会话的基类（任务执行器接口）。"""

    def __init__(
        self,
        base_cfg,
        name="BaseSession",
        command_source=None,
        frame_manager=None,
        adapter=None,
        capture_meta_store=None,
    ):
        self.name = name
        self.base_cfg = base_cfg
        self.command_source = command_source if command_source is not None else _noop_command_source
        self.frame_manager = frame_manager or FrameManager()  # 观测帧缓存（preview / WebRTC 消费）
        # 节点级 active adapter（注入，生命周期归节点）：会话只引用，不持有 / 不释放
        self.adapter = adapter
        # 采集元信息选项存储（config/capture.yml）：capture meta 配置命令任务态读写；
        # 缺省用默认路径（与节点 / server 同源），测试可注入临时 store。
        self.capture_meta_store = capture_meta_store
        # 退出命令（session quit）：submit 通道命令由节点任务结束后补发回执
        self.exit_command = None
        # 外部请求停止标志（node 失联 ERROR 时终止仍在运行的任务线程用）
        self._stop_requested = False

    def stop(self):
        """请求会话停止：设置标志，会话主循环检查后尽快返回（RunResult.ERROR）。

        供 node 失联 / 健康失败进入 ERROR 时终止仍在运行的任务线程——否则会话循环
        会与 node 主循环竞争消费总线命令，把 node reset 等恢复命令拦截为 rejected。
        """
        self._stop_requested = True

    def session_start(self):
        """连接硬件、初始化会话（节点进入 ACTIVE 前调用）。"""
        pass

    def session_finish(self):
        """释放资源（节点释放会话时调用）。"""
        pass

    def safe_stop(self):
        """安全停止（幂等、失败安全）：委托给机器人适配器的 safe_stop；无适配器时 no-op。"""
        adapter = getattr(self, "adapter", None)
        if adapter is not None:
            try:
                adapter.safe_stop()
            except Exception as exc:
                debug_print(self.name, f"safe_stop failed: {exc}", "ERROR")

    def _wait_ready(self, exit_cmd_name: str) -> RunResult | None:
        """阻塞等待机器人就绪（``adapter.ready``）。期间响应命令：

        - ``exit_cmd_name``（session quit 退出整个会话）→ 记录退出命令并返回 ``FINISHED``；
        - 急停 → 安全停止并返回 ``ERROR``；
        - 复位（robot reset）→ 重新 ``adapter.reset()``。
        就绪后返回 ``None``，调用方继续任务流程。
        """
        debug_print(self.name, "Waiting for robot ready...", "INFO")
        while not self.adapter.ready:
            if self._stop_requested:
                return RunResult.ERROR
            debug_print(self.name, "Robot not started yet, verify hardware.", "WARNING")
            cmd = self.command_source()
            name = _cmd_name(cmd)
            if name == exit_cmd_name:
                self._record_exit(cmd)
                return RunResult.FINISHED
            if name == CMD_ROBOT_ESTOP:
                self.safe_stop()
                return RunResult.ERROR
            if name == CMD_ROBOT_RESET:
                self.adapter.reset()
                self._reply(cmd, ok_result(state="ready"))
            elif cmd is not None:  # 未识别命令（等待就绪阶段不适用）统一回执，避免 submit 挂起
                self._reply(cmd, CommandResult(status="rejected", error=f"{name} not applicable", status_code=409))
            time.sleep(1)
        return None

    def _on_infer_endpoint(self, cmd):
        """处理推理端点配置命令（infer ip / infer ip set / infer port / infer port set）。

        会话运行期间（ACTIVE）命令由会话循环 poll，本方法让配置命令在任务态也可用——
        委托 ``utils.commands.handle_infer_endpoint``（写内存态 ``base_cfg["policy"]``），
        与节点主循环（非任务态）共用同一逻辑，保证「任何状态可用」。
        """
        return handle_infer_endpoint(self.base_cfg, cmd)

    def _on_capture_meta(self, cmd):
        """处理采集元信息选项命令（capture meta list/add/edit/delete/delete-key）。

        会话运行期间（ACTIVE）命令由会话循环 poll，本方法让配置命令在任务态也可用——
        委托 ``utils.commands.handle_capture_meta``（读写 config/capture.yml），与节点主循环
        （非任务态）共用同一逻辑，保证「任何状态可用」。
        """
        return handle_capture_meta(cmd, self.capture_meta_store)

    def _record_exit(self, cmd) -> None:
        """记录退出命令（仅 submit 通道命令需节点补发回执），由节点任务结束后补发。"""
        if cmd is not None and getattr(cmd, "reply_to", None) is not None:
            self.exit_command = cmd

    def _reply(self, cmd, result) -> None:
        """统一回执：命令携带 reply_to（submit 通道）则回调结果。"""
        if cmd is not None and getattr(cmd, "reply_to", None) is not None:
            cmd.reply_to(result)

    def _execute_action(self, cmd) -> None:
        """robot execute：解析 qpos 位置参数 → ``adapter.execute(qpos)``（维度校验在 adapter）。

        参数缺失 / 非法 / 维度不符 → 回执 rejected（不崩溃）；成功 → 回执 ok（回显 action）。
        """
        try:
            qpos = parse_qpos(cmd.params.get("qpos"))
            self.adapter.execute(qpos)
        except ValueError as exc:
            self._reply(cmd, CommandResult(status="rejected", error=str(exc), status_code=400))
            return
        self._reply(cmd, ok_result(state="ready", action=qpos))

    def _set_teleop(self, cmd) -> None:
        """robot teleop：解析 true/false 参数 → ``adapter.set_teleop``（遥操作开关）。

        参数缺失 / 非法 → 回执 rejected（不崩溃）；成功 → 回执 ok（回显 teleop）。
        """
        try:
            enabled = parse_bool(cmd.params.get("enabled"))
            self.adapter.set_teleop(enabled)
        except ValueError as exc:
            self._reply(cmd, CommandResult(status="rejected", error=str(exc), status_code=400))
            return
        self._reply(cmd, ok_result(state="ready", teleop=enabled))

    def run(self):
        """阻塞式会话执行（节点进入 ACTIVE 时调用），返回 RunResult。"""
        return RunResult.FINISHED
