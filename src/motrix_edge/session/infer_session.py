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

import numpy as np

from motrix_edge.adapter import AdapterCapability
from motrix_edge.policy import get_policy
from motrix_edge.utils.commands import (
    CMD_INFER_CONNECT,
    CMD_INFER_IP,
    CMD_INFER_IP_SET,
    CMD_INFER_PORT,
    CMD_INFER_PORT_SET,
    CMD_INFER_ROLLOUT,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_RESET,
    CMD_ROBOT_TELEOP,
    CMD_SESSION_QUIT,
    ROLLOUT_MODE_CONTINUOUS,
    ROLLOUT_MODE_DRAIN,
    CommandResult,
    ok_result,
    parse_rollout_mode,
)
from motrix_edge.utils.data_handler import debug_print

from .base import BaseSession, RunResult, SessionState, _cmd_name


class InferSession(BaseSession):
    """推理会话 —— 组合 RobotAdapter + 推理策略客户端的推理执行器（无回合概念）。

    生命周期由 EdgeNode 管理（session_start → run → session_finish）：会话由外部
    infer rollout 步进驱动（上传观测 → 推理 → 下发动作）。命令：infer rollout 单步闭环、
    session quit 退出、robot estop 急停、robot reset 复位。
    """

    def __init__(self, base_cfg, command_source=None, frame_manager=None, adapter=None, policy_type=None):
        super().__init__(
            base_cfg=base_cfg,
            name="InferSession",
            command_source=command_source,
            frame_manager=frame_manager,
            adapter=adapter,
        )
        if self.adapter is None:
            raise ValueError("infer session requires an injected adapter (owned by node)")
        if not self.adapter.capabilities.supports(AdapterCapability.EXECUTE):
            raise ValueError("injected adapter does not support EXECUTE capability")
        self.policy_config = self.base_cfg.get("policy", {})
        # 运行时策略选择：session run infer 携带 policy_type（HTTP / 命令）；由节点校验
        self.policy_type = policy_type
        self.policy = get_policy(base_cfg, policy_type=self.policy_type)

        self.state = SessionState.INIT  # 实时状态（供外部查询）
        self._connected = False  # 策略服务器连接状态（infer connect 成功后置 True）

        debug_print(self.name, f"Policy config: {self.policy_config} (type={self.policy_type})", "INFO")

    @property
    def connected(self) -> bool:
        """策略服务器是否已连接（``infer connect`` 成功后为 True）。"""
        return self._connected

    def session_start(self):
        """进入会话（节点进入 ACTIVE 前调用）：adapter 已由节点 discover 绑定。

        推理节点**不自动连接**：连接推迟到显式 ``infer connect`` 命令（单次尝试），
        避免同步连接阻塞 ``session run`` 命令回执 / 无限重试。
        """
        self.state = SessionState.READY

    def session_finish(self):
        """释放资源（节点释放会话时调用）。adapter 由节点持有，不在此释放。"""
        self.policy.disconnect()
        self._connected = False
        self.state = SessionState.FINISHED

    def _connect_policy(self, cmd) -> None:
        """infer connect：单次尝试连接推理节点。

        成功 → 回执 ok（含服务端 metadata）；失败 → 回执 error（连接状态保持未连接，
        ``infer rollout`` 在未连接时 503）。
        """
        try:
            self.policy.connect()
            self._connected = True
            metadata = dict(getattr(self.policy, "server_metadata", None) or {})
            self._reply(cmd, ok_result(state="ready", connected=True, metadata=metadata))
            debug_print(self.name, f"Inference server connected: {metadata}", "INFO")
        except Exception as exc:  # noqa: BLE001 单次尝试失败：保持未连接，不无限重试
            self._connected = False
            debug_print(self.name, f"infer connect failed: {exc}", "WARNING")
            self._reply(cmd, CommandResult(status="error", error=f"infer connect failed: {exc}", status_code=502))

    def run(self):
        """阻塞式推理主循环：等待就绪 → 显式 infer connect → 等待 infer rollout 步进闭环。"""
        # 复位（reset() 非阻塞设 home 目标）
        self.adapter.reset()
        self.policy.reset()
        # 等待机器人就绪（期间可 session quit 退出 / robot estop 急停 / robot reset 复位）
        result = self._wait_ready(CMD_SESSION_QUIT)
        if result is not None:
            self.state = SessionState.FINISHED if result == RunResult.FINISHED else SessionState.ERROR
            return result
        self.state = SessionState.READY

        debug_print(
            self.name,
            "Robot READY. infer connect to link, infer rollout to step, session quit to exit.",
            "INFO",
        )
        while True:
            if self._stop_requested:  # 外部请求停止（node 失联 ERROR）：立即退出
                self.state = SessionState.ERROR
                return RunResult.ERROR
            cmd = self.command_source()
            name = _cmd_name(cmd)
            if name == CMD_INFER_CONNECT:  # 显式连接推理节点（单次尝试，可反复触发重连）
                self._connect_policy(cmd)
            elif name == CMD_INFER_ROLLOUT:  # 推理闭环：count 次数 / continuous 持续 / drain 消耗块
                if not self._connected:
                    self._reply(
                        cmd,
                        CommandResult(
                            status="rejected",
                            error="policy not connected (run infer connect)",
                            status_code=503,
                        ),
                    )
                    continue
                try:
                    mode, count = parse_rollout_mode(cmd.params.get("count"))
                except ValueError as exc:
                    self._reply(cmd, CommandResult(status="rejected", error=str(exc), status_code=400))
                    continue
                if mode == ROLLOUT_MODE_CONTINUOUS:  # 持续推理：启动即回执，直到 session quit / estop
                    self._reply(cmd, ok_result(state="continuous", started=True))
                    result = self._run_continuous()
                    self.state = SessionState.FINISHED if result == RunResult.FINISHED else SessionState.ERROR
                    return result
                if mode == ROLLOUT_MODE_DRAIN:  # 只消耗当前缓存动作块（不发新推理请求）
                    self._run_drain(cmd)
                    continue
                self._run_count(cmd, count)  # 推理 N 次（缺省 1）
            elif name == CMD_SESSION_QUIT:  # 退出推理会话
                self.adapter.reset()  # 推理结束回到 home
                self.state = SessionState.FINISHED
                self._record_exit(cmd)
                debug_print(self.name, "Inference finished, robot reset to home.", "INFO")
                return RunResult.FINISHED
            elif name == CMD_ROBOT_ESTOP:  # 急停：立即安全停止再进 ERROR
                self.safe_stop()
                self.state = SessionState.ERROR
                self._reply(cmd, CommandResult(status="error", error="estop", status_code=500))
                return RunResult.ERROR
            elif name == CMD_ROBOT_RESET:  # 复位（会话期间）
                self.adapter.reset()
                self._reply(cmd, ok_result(state="ready"))
            elif name == CMD_ROBOT_EXECUTE:  # 直接下发 raw 动作（qpos 直接作为参数）
                self._execute_action(cmd)
            elif name == CMD_ROBOT_TELEOP:  # 遥操作开关（true/false 直接作为参数）
                self._set_teleop(cmd)
            elif name in (  # 配置级命令：任务态也可用（infer ip / infer ip set / infer port / infer port set）
                CMD_INFER_IP,
                CMD_INFER_IP_SET,
                CMD_INFER_PORT,
                CMD_INFER_PORT_SET,
            ):
                self._reply(cmd, self._on_infer_endpoint(cmd))
            else:  # 未识别命令（当前任务不适用）统一回执，避免 submit 挂起
                if cmd is not None:
                    self._reply(cmd, CommandResult(status="rejected", error=f"{name} not applicable", status_code=409))
                time.sleep(0.02)  # 无命令时轻量轮询（避免忙等）

    def _run_count(self, cmd, count: int) -> None:
        """infer rollout <N>：连续执行 N 次 观测 → 推理 → 动作下发，回执动作列表。"""
        actions = []
        for _ in range(count):
            obs = self.adapter.observe()  # 推理输入（显示观测由节点级写入 frame_manager）
            if obs is None:
                self._reply(cmd, CommandResult(status="rejected", error="observation not ready", status_code=503))
                break
            action = self.policy.infer(obs)
            if action is not None:
                self.adapter.rollout(action)  # 解析模型 action 为限速目标并推进一帧
            actions.append(self._action_repr(action))
            time.sleep(0.33)  # 轻量轮询，避免忙等
        else:
            debug_print(self.name, f"Rollout executed {count} step(s).", "INFO")
            self._reply(
                cmd,
                ok_result(
                    state="ready",
                    count=count,
                    action=actions[-1] if actions else None,
                    actions=actions,
                ),
            )

    def _run_drain(self, cmd) -> None:
        """infer rollout drain：只消费当前缓存动作块（不发新推理请求），回执消耗步数。"""
        actions = []
        while True:
            obs = self.adapter.observe()
            if obs is None:
                self._reply(cmd, CommandResult(status="rejected", error="observation not ready", status_code=503))
                return
            action = self.policy.drain(obs)  # 缓存块耗尽返回 None → 停止
            if action is None:
                break
            self.adapter.rollout(action)
            actions.append(self._action_repr(action))
            time.sleep(0.33)
        debug_print(self.name, f"Drain consumed {len(actions)} step(s).", "INFO")
        self._reply(
            cmd,
            ok_result(state="ready", count=len(actions), action=actions[-1] if actions else None, actions=actions),
        )

    def _run_continuous(self) -> RunResult:
        """infer rollout continuous：持续推理，每步轮询命令响应退出 / 复位 / 急停。

        启动命令已回执 started；持续直到 session quit（FINISHED）/ robot estop（ERROR）/
        node 失联（ERROR）。返回 RunResult（由调用方置会话状态）。
        """
        debug_print(self.name, "Continuous rollout started (session quit to stop).", "INFO")
        while True:
            if self._stop_requested:  # 外部请求停止（node 失联 ERROR）：立即退出
                return RunResult.ERROR
            cmd = self.command_source()
            name = _cmd_name(cmd)
            if name == CMD_SESSION_QUIT:  # 停止持续推理并退出会话
                self.adapter.reset()  # 推理结束回到 home
                self._record_exit(cmd)
                debug_print(self.name, "Continuous rollout stopped, inference finished.", "INFO")
                return RunResult.FINISHED
            if name == CMD_ROBOT_ESTOP:  # 急停
                self.safe_stop()
                self._reply(cmd, CommandResult(status="error", error="estop", status_code=500))
                return RunResult.ERROR
            if name == CMD_ROBOT_RESET:  # 持续中复位（回执 ok，继续推理）
                self.adapter.reset()
                self._reply(cmd, ok_result(state="continuous"))
                continue
            if name == CMD_INFER_ROLLOUT:  # 持续中重复 rollout：拒绝
                self._reply(
                    cmd,
                    CommandResult(status="rejected", error="continuous rollout already running", status_code=409),
                )
                continue
            if cmd is not None:  # 持续中其它命令：拒绝（避免 submit 挂起）
                self._reply(
                    cmd,
                    CommandResult(
                        status="rejected",
                        error=f"{name} not applicable during continuous rollout",
                        status_code=409,
                    ),
                )
            obs = self.adapter.observe()
            if obs is None:  # 观测未就绪：轻量轮询
                time.sleep(0.33)
                continue
            action = self.policy.infer(obs)
            if action is not None:
                self.adapter.rollout(action)
            time.sleep(0.33)

    @staticmethod
    def _action_repr(action):
        """动作 → JSON 可表达（list）；None 保持 None。"""
        if action is None:
            return None
        return np.asarray(action).reshape(-1).tolist()
