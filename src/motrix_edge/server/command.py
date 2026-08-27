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
motrix_edge_lease.md）。当前实现（capability 映射）：
  - ``estop``        → push ``Command(robot estop)``（全局急停，安全停止 + 节点转 ERROR）；
  - ``reset``        → push ``Command(node reset)``（节点复位，ERROR → IDLE）；
  - ``robot_reset``  → push ``Command(robot reset)``（机器人复位，adapter.reset）；
  - ``robot_execute``→ submit ``Command(robot execute, params={qpos})``（直接下发 raw 动作，
                       回执透传；维度校验在 adapter.execute）；
  - ``robot_teleop`` → push ``Command(robot teleop, params={enabled})``（遥操作开关，true/false）；
  - 其他 capability → 骨架（accepted，预留 Capability 校验 / 具体下发执行）。

``idempotency_key``（HTTP 请求体字段）为 Console 幂等契约**预留**：当前**未实现**去重
（并发 check-dispatch-put 非原子，无法保证同 key 只下发一次），调用方须自行处理重试、
勿依赖去重。
"""

from motrix_edge.lease import LeaseError, LeaseManager
from motrix_edge.utils.commands import (
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_EPISODE_START,
    CMD_INFER_IP,
    CMD_INFER_IP_SET,
    CMD_INFER_PORT,
    CMD_INFER_PORT_SET,
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
            # 机器人复位（adapter.reset，非节点复位）：READY / ACTIVE 下适用
            self._bus.push(Command(CMD_ROBOT_RESET, meta={"lease_id": lease_id}))
            return {"status": "accepted", "command_id": command_id, "executed": "robot_reset"}

        if capability == "robot_execute":
            # 直接下发 raw 动作：qpos 直接作为参数；**submit 同步等回执**（维度校验在
            # adapter.execute，失败回执 rejected 透传给前端，避免 push 静默丢失）
            qpos = (params or {}).get("qpos")
            try:
                result = self._bus.submit(
                    Command(CMD_ROBOT_EXECUTE, params={"qpos": qpos}, meta={"lease_id": lease_id}),
                    timeout=5.0,
                )
            except Exception as exc:  # noqa: BLE001 submit 超时（命令未被消费）→ HTTP 错误
                raise CommandError(str(exc), status_code=504) from exc
            return {
                "status": result.status,
                "command_id": command_id,
                "executed": "robot_execute",
                "data": result.data,
                "error": result.error,
            }

        if capability == "robot_teleop":
            # 遥操作开关：true / false 直接作为参数（adapter.set_teleop）
            enabled = (params or {}).get("enabled")
            self._bus.push(Command(CMD_ROBOT_TELEOP, params={"enabled": enabled}, meta={"lease_id": lease_id}))
            return {"status": "accepted", "command_id": command_id, "executed": "robot_teleop"}

        if capability == "capture_episode_start":
            # 开始一轮采集（episode 开始）：adapter.start_capture（采集会话内消费）
            self._bus.push(Command(CMD_CAPTURE_EPISODE_START, meta={"lease_id": lease_id}))
            return {"status": "accepted", "command_id": command_id, "executed": "capture_episode_start"}

        if capability == "capture_episode_end":
            # 结束一轮采集（episode 结束）：adapter.end_capture（采集会话内消费）
            self._bus.push(Command(CMD_CAPTURE_EPISODE_END, meta={"lease_id": lease_id}))
            return {"status": "accepted", "command_id": command_id, "executed": "capture_episode_end"}

        # 推理端点配置（infer ip / infer port get/set）：配置级命令经同一命令总线
        # （submit 同步回执），与本地 CLI 行为一致；写入内存态 policy 段，下次
        # session run infer 生效。
        if capability == "infer_ip":
            return self._infer_endpoint_cmd(command_id, "infer_ip", CMD_INFER_IP, lease_id=lease_id)
        if capability == "infer_port":
            return self._infer_endpoint_cmd(command_id, "infer_port", CMD_INFER_PORT, lease_id=lease_id)
        if capability == "infer_ip_set":
            return self._infer_endpoint_cmd(
                command_id,
                "infer_ip_set",
                CMD_INFER_IP_SET,
                params={"ip": (params or {}).get("ip")},
                lease_id=lease_id,
            )
        if capability == "infer_port_set":
            return self._infer_endpoint_cmd(
                command_id,
                "infer_port_set",
                CMD_INFER_PORT_SET,
                params={"port": (params or {}).get("port")},
                lease_id=lease_id,
            )

        # 其他 capability：骨架（预留 Capability 校验 / 下发机器人执行）
        return {"status": "accepted", "command_id": command_id, "executed": None}

    def _infer_endpoint_cmd(self, command_id, executed, name, params=None, lease_id=None) -> dict:
        """推理端点配置命令：submit 同步等回执（node 主循环消费），回执透传。"""
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
