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

"""routes.leases —— ``/v1/leases/*`` 的 HTTP 映射（Edge 级租约）。

Console 权威签发 → Edge 只保留**镜像** + 校验（Edge 不生成 lease_id）；受控操作凭
``X-Lease-Id`` 走 ``LeaseManager.require``。``LeaseError`` 由 app 层统一错误处理器转 HTTP
（缺失 409 / 不匹配 403 / 过期 410），故此处不 try/except。
"""

from fastapi import APIRouter

from motrix_edge.lease import BEIJING_TZ, Lease
from motrix_edge.server.deps import Services
from motrix_edge.server.schemas import LeaseInstallRequest, LeaseRenewRequest


def build_router(services: Services) -> APIRouter:
    """构建 Edge 级租约路由（install / renew / revoke / 查询）。"""
    router = APIRouter()
    leases = services.leases

    @router.post("/v1/leases")
    def leases_install(req: LeaseInstallRequest):
        """Console 生成租约并下发，Edge 接收保存本地**镜像**（Console 权威）。

        Edge 不生成 lease_id，只保留 + 校验；已有活跃控制租约 → 409。
        """
        leases.install(
            Lease(
                lease_id=req.lease_id,
                edge_id=req.edge_id,
                holder_subject_id=req.holder_subject_id,
                purpose=req.purpose,
                state=req.state,
                expires_at=req.expires_at,
                lease_version=req.lease_version,
                ttl=req.ttl,
            )
        )
        return {"status": "accepted", **leases.status()}

    @router.post("/v1/leases/{lease_id}:renew")
    def leases_renew(lease_id: str, req: LeaseRenewRequest):
        """Console 续约：lease_version 递增（版本回退拒绝），Edge 更新本地镜像。

        续约 = 以更高 ``lease_version`` 原地延长 ``expires_at``；Edge 在旧租约到期前
        收到新镜像即可保持控制。
        """
        lease = leases.renew(lease_id, req.lease_version, req.expires_at)
        return {
            "status": "accepted",
            "lease_id": lease.lease_id,
            "lease_version": lease.lease_version,
            "state": lease.state.value,
            "expires_at": lease.expires_at.astimezone(BEIJING_TZ).isoformat(),
        }

    @router.get("/v1/leases/{lease_id}")
    def leases_get(lease_id: str):
        """查询 Edge 本地 lease 镜像状态：``200``（返回 lease 信息）/ ``404``（不存在）。"""
        return leases.mirror(lease_id)

    @router.post("/v1/leases/{lease_id}:revoke")
    def leases_revoke(lease_id: str):
        """Console 撤销租约：Edge 进入无效状态（Revoked），不能执行受限操作。"""
        lease = leases.revoke(lease_id)
        return {"status": "accepted", "lease_id": lease.lease_id, "state": lease.state.value}

    @router.get("/v1/leases")
    def leases_status():
        """租约状态汇总（只读，Edge 侧）：当前租约 / leasable / renew_interval。"""
        return leases.status()

    return router
