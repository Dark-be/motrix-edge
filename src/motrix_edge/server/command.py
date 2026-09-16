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

"""CommandService —— /v1/commands 控制器：桥接 HTTP 命令到「正在运行的 EdgeNode」。

受控操作（命令）须持有 Edge 级活跃租约（经 ``LeaseManager`` 校验，见 wiki/design
motrix_edge_lease.md）。当前实现（capability 映射）分两类通道：

**submit（同步等回执，回执透传）**——操作类命令一律走这条，调用方拿得到成功 / 失败与结果：
  - ``robot_reset``  → ``robot reset``（机器人复位，adapter.reset）；
  - ``robot_execute``→ ``robot execute, params={qpos}``（直接下发 raw 动作，维度校验在 adapter）；
  - ``robot_teleop`` → ``robot teleop, params={enabled, mode?}``（遥操作 / 人工接管，
    ``mode=delta`` 为增量接管；回执回显生效的 ``teleop`` / ``mode``）；
  - ``capture_episode_start`` / ``capture_episode_end`` → 一轮采集（episode）开始 / 结束；
  - ``capture_sync`` / ``infer_connect`` / ``infer_ip*`` / ``infer_port*``（配置与连接类）。

**push（即发即忘，无回执）**——仅留给「不能等 / 不该等」的路径：
  - ``estop``        → ``robot estop``（全局急停，安全停止 + 节点转 ERROR）；
  - ``reset``        → ``node reset``（节点复位，ERROR → IDLE；ERROR 下由节点恢复路径消费）。

- 其他 capability → 骨架（accepted，预留 Capability 校验 / 具体下发执行）。

``idempotency_key``（HTTP 请求体字段）为 Console 幂等契约**预留**：当前**未实现**去重
（并发 check-dispatch-put 非原子，无法保证同 key 只下发一次），调用方须自行处理重试、
勿依赖去重。
"""

import json

from motrix_edge.lease import LeaseError, LeaseManager
from motrix_edge.utils.commands import (
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_EPISODE_START,
    CMD_CAPTURE_SYNC,
    CMD_INFER_CONNECT,
    CMD_NODE_RESET,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_RESET,
    CMD_ROBOT_TELEOP,
    Command,
)


class CommandError(Exception):
    """命令被拒绝（租约缺失 / 不匹配 / 过期等）。携带 HTTP status_code。"""

    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


class CommandService:
    """HTTP commands → 租约校验 + 信号总线（estop 等）的桥接控制器。"""

    def __init__(self, node, bus, leases: LeaseManager | None = None):
        self._node = node  # 正在运行的 EdgeNode（由 node 程序主线程持有）
        self._bus = bus  # 共享信号总线：web / CLI 线程 push，EdgeNode 主循环 poll
        self._leases = leases or LeaseManager()  # Edge 级租约（受控命令校验用）

    def execute(
        self,
        command_id: str,
        lease_id: str | None = None,
        capability: str | None = None,
        params: dict | None = None,
    ) -> dict:
        """执行命令：先校验租约；``capability=estop`` → 急停信号。

        幂等（idempotency_key）：**预留、未实现** —— 本方法不做去重（并发下无法保证同
        key 只下发一次），见模块 docstring；调用方需自行处理重试。

        返回 ``{"status": "accepted", "command_id": ..., "executed": ...}``。
        """
        try:
            self._leases.require(lease_id)
        except LeaseError as exc:
            raise CommandError(str(exc), status_code=exc.status_code) from exc

        if capability == "estop":
            self._bus.push(Command(CMD_ROBOT_ESTOP, meta={"lease_id": lease_id}))  # 全局急停：node 安全停止 + 转 ERROR
            return {"status": "accepted", "command_id": command_id, "executed": "estop"}

        if capability == "reset":
            # 节点复位：ERROR → IDLE（释放 adapter 重新探测；node reset 仅 ERROR 下适用）
            self._bus.push(Command(CMD_NODE_RESET, meta={"lease_id": lease_id}))
            return {"status": "accepted", "command_id": command_id, "executed": "reset"}

        if capability == "robot_reset":
            # 机器人复位（adapter.reset，非节点复位）：READY / ACTIVE 下适用；submit 等回执
            return self._submit_cmd(command_id, "robot_reset", CMD_ROBOT_RESET, lease_id=lease_id)

        if capability == "robot_execute":
            # 直接下发 raw 动作：qpos 直接作为参数；submit 同步等回执（维度校验在 adapter.execute，
            # 失败回执 rejected 透传给前端，避免 push 静默丢失）
            return self._submit_cmd(
                command_id,
                "robot_execute",
                CMD_ROBOT_EXECUTE,
                params={"qpos": (params or {}).get("qpos")},
                lease_id=lease_id,
            )

        if capability == "robot_teleop":
            # 遥操作 / 人工接管：enabled=true/false，可选 mode=absolute|delta 进同一命令。
            # submit 等回执：回显实际生效的 teleop / mode（参数非法由命令处理器 rejected）
            target = params or {}
            return self._submit_cmd(
                command_id,
                "robot_teleop",
                CMD_ROBOT_TELEOP,
                params={"enabled": target.get("enabled"), "mode": target.get("mode")},
                lease_id=lease_id,
            )

        if capability == "capture_episode_start":
            # 开始一轮采集（episode 开始）：adapter.start_capture（采集会话内消费）；
            # submit 等回执（回显 episode=start / recording）
            return self._submit_cmd(command_id, "capture_episode_start", CMD_CAPTURE_EPISODE_START, lease_id=lease_id)

        if capability == "capture_episode_end":
            # 结束一轮采集（episode 结束）：adapter.end_capture（采集会话内消费）；submit 等回执
            return self._submit_cmd(command_id, "capture_episode_end", CMD_CAPTURE_EPISODE_END, lease_id=lease_id)

        if capability == "infer_connect":
            # 单次尝试连接推理节点（推理会话内消费；submit 同步等回执）
            return self._submit_cmd(command_id, "infer_connect", CMD_INFER_CONNECT, lease_id=lease_id)

        if capability == "capture_sync":
            # 同步采集元信息到机器人进程（采集会话 / 推理录制会话内消费；submit 等回执）
            return self._submit_cmd(
                command_id,
                "capture_sync",
                CMD_CAPTURE_SYNC,
                params={"meta": json.dumps((params or {}).get("meta"))},
                lease_id=lease_id,
            )

        # 其他 capability：骨架（预留 Capability 校验 / 下发机器人执行）
        return {"status": "accepted", "command_id": command_id, "executed": None}

    def _submit_cmd(
        self, command_id: str, executed: str, name: str, params: dict | None = None, lease_id: str | None = None
    ) -> dict:
        """submit 同步等回执（node 主循环 / 会话循环消费），回执透传给调用方。

        命令未被消费（超时）→ ``504``；业务层拒绝（参数非法 / 状态不符）按**回执**返回
        （HTTP 200 + ``status=rejected`` + ``error``）——调用方按 ``status`` / ``error`` 判定，
        与 CLI 回执语义一致（本方法只负责通道，不替处理器做校验）。
        """
        try:
            result = self._bus.submit(Command(name, params=params or {}, meta={"lease_id": lease_id}), timeout=5.0)
        except Exception as exc:  # noqa: BLE001 submit 超时（命令未被消费）→ HTTP 错误
            raise CommandError(str(exc), status_code=504) from exc
        return {
            "status": result.status,
            "command_id": command_id,
            "executed": executed,
            "data": result.data,
            "error": result.error,
        }
