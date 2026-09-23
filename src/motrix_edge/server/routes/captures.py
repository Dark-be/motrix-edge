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

"""routes.captures —— ``/v1/captures/*`` 与 ``/v1/preview`` 的 HTTP 映射。

- **写**（enter / exit / sync）：经 ``CommandService`` 提交命令（``session run capture`` /
  ``session quit`` / ``capture sync``），由 EdgeNode 主循环 / 采集会话消费——与 CLI 同一批命令；
- **读**（status / precheck）：``server/status.py`` 的只读快照（读 node 内存状态，不产生副作用）；
- **采集元信息选项**（meta）：``server/meta.py`` 直连 ``CaptureMetaStore``（配置级、有意不经总线）；
- ``/v1/preview``：独立于会话的观测预览（``PreviewService`` 直接读 node.frame_manager 缓存）。

未注入对应服务 → 501。
"""

from fastapi import APIRouter, Header

from motrix_edge.command import CMD_CAPTURE_SYNC, CMD_SESSION_QUIT, CMD_SESSION_RUN
from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.server.deps import Services
from motrix_edge.server.routes._common import accepted
from motrix_edge.server.schemas import (
    CaptureMetaAddRequest,
    CaptureMetaEditRequest,
    CaptureSyncRequest,
)
from motrix_edge.server.status import capture_precheck, capture_snapshot


def build_router(services: Services) -> APIRouter:
    """构建采集会话、观测预览与采集元信息路由。"""
    router = APIRouter()
    node = services.node
    commands = services.commands
    leases = services.leases
    meta = services.meta
    preview = services.preview

    def _node():
        if node is None:
            raise ServiceError("captures not enabled (node not injected)", code=ErrorCode.NOT_IMPLEMENTED)
        return node

    def _commands():
        if commands is None:
            raise ServiceError("captures not enabled (CommandService not injected)", code=ErrorCode.NOT_IMPLEMENTED)
        return commands

    def _meta():
        if meta is None:
            raise ServiceError(
                "captures meta not enabled (CaptureMetaService not injected)", code=ErrorCode.NOT_IMPLEMENTED
            )
        return meta

    @router.get("/v1/captures")
    def captures_status():
        """状态快照：node_state / session_type / state / adapter（含遥操作位）/ capture_status / disk / lease_id。"""
        return capture_snapshot(_node(), leases)

    @router.get("/v1/captures/precheck")
    def captures_precheck():
        """预检（只读）：机器人就绪 + 磁盘 + 当前租约 / 可租状态。"""
        return capture_precheck(_node(), leases)

    @router.get("/v1/preview")
    def captures_preview(x_lease_id: str | None = Header(default=None)):
        """最新观测预览：qpos / action 状态 + 摄像头名列表（图像走 WebRTC，不内联）。

        独立于采集 / 推理会话（PreviewService 直接读 node.frame_manager 观测缓存）：
        不要求会话，预览随时可开；受控操作：须持有有效租约（X-Lease-Id）。
        """
        if preview is None:
            raise ServiceError("preview not enabled", code=ErrorCode.NOT_IMPLEMENTED)
        return preview.preview(lease_id=x_lease_id)

    @router.get("/v1/captures/meta")
    def captures_meta():
        """采集元信息选项（config/capture.yml 的 ``meta`` 段；前端选择列表用，只读）。"""
        return _meta().list()

    @router.post("/v1/captures/meta")
    def captures_meta_add(req: CaptureMetaAddRequest, x_lease_id: str | None = Header(default=None)):
        """新增采集元信息选项（分类不存在则自动创建）；重复 → 400。须持租约。

        回执与 ``GET /v1/captures/meta`` 同构（``{meta: 全量}``），前端写后无需再拉取。
        选项管理是**配置级**操作，与机器人进程 / 会话状态无关（不进状态机）。
        """
        return _meta().add(req.key, req.value, lease_id=x_lease_id)

    @router.patch("/v1/captures/meta")
    def captures_meta_edit(req: CaptureMetaEditRequest, x_lease_id: str | None = Header(default=None)):
        """重命名采集元信息选项（``key`` 下 ``old`` → ``new``）；不存在 / 重复 → 400。须持租约。"""
        return _meta().edit(req.key, req.old, req.new, lease_id=x_lease_id)

    @router.delete("/v1/captures/meta")
    def captures_meta_delete(key: str, value: str, x_lease_id: str | None = Header(default=None)):
        """删除采集元信息选项（分类清空则一并删除该分类）；不存在 → 400。须持租约。

        ``key`` / ``value`` 经 **query 参数**提交（选项值可能含空格 / 中文）。
        删除整个分类见 ``DELETE /v1/captures/meta/{key}``。
        """
        return _meta().delete(key, value, lease_id=x_lease_id)

    @router.delete("/v1/captures/meta/{key}")
    def captures_meta_delete_key(key: str, x_lease_id: str | None = Header(default=None)):
        """删除整个采集元信息分类；分类不存在 → 400。须持租约。"""
        return _meta().delete_key(key, lease_id=x_lease_id)

    @router.post("/v1/captures/sync")
    def captures_sync(req: CaptureSyncRequest, x_lease_id: str | None = Header(default=None)):
        """同步采集元信息（采集员 / 任务名等）到机器人进程：进程保存一轮数据时附加。

        受控操作：须持有有效租约（X-Lease-Id）；采集会话内消费。
        """
        return accepted(_commands().submit(CMD_CAPTURE_SYNC, {"meta": req.meta}, lease_id=x_lease_id))

    @router.post("/v1/captures")
    def captures_enter(x_lease_id: str | None = Header(default=None)):
        """创建采集会话（进入任务环境）：READY → ACTIVE，需先持有有效租约（X-Lease-Id）。

        单 adapter 包：无 adapter 选择，采集基于节点绑定的唯一 adapter；已在环境中 → 409。
        采集为观测会话（无回合流程控制）：进入后持续读共享内存观测，session quit 退出。
        """
        return accepted(_commands().submit(CMD_SESSION_RUN, {"session": "capture"}, lease_id=x_lease_id))

    @router.delete("/v1/captures")
    def captures_exit(lease_id: str | None = None):
        """退出采集任务环境（ACTIVE → IDLE）。租约经 query 参数 `lease_id` 提交校验。

        租约不随退出销毁 —— 生命周期由 Edge 级 `/v1/leases/*`（install / renew / revoke）管理，
        session 只消费（校验）租约。
        """
        # session quit 退出任务：节点在任务结束后补发「node ready」回执（超时给足）
        return accepted(_commands().submit(CMD_SESSION_QUIT, lease_id=lease_id, timeout=10.0))

    return router
