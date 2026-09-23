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

"""routes.health —— ``/v1/health`` 与 ``/v1/adapters/*`` 的 HTTP 映射。

探活与适配器清单**一律只读 node 内存状态**（node 主循环已周期 discover / 心跳并缓存），
不实时 discover——避免前端轮询持续对 SDK 进程发请求。
"""

import shutil
from datetime import datetime

from fastapi import APIRouter, Header

from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.lease import BEIJING_TZ
from motrix_edge.server.deps import Services
from motrix_edge.server.schemas import AdapterConfigRequest
from motrix_edge.server.state import adapter_ref
from motrix_edge.utils.version import get_package_version


def _adapters(node) -> dict:
    """Edge 适配器列表（health 展示 / 客户端选择）。

    - robots：node 当前绑定的唯一机器人（``[{name, type}]``，单 adapter 包；只读
      node 内存状态，**不实时 discover**）。
    - policies：全部已注册策略适配器（``[{type, class, module, config_items}]``，前端策略
      选择用；``config_items`` 为该策略的配置项 schema——前端选择策略后**动态渲染表单**、
      进入会话前即可填写；不触发第三方包导入）。
    """
    from motrix_edge.policy import policy_adapters, policy_config_items

    robots = []
    if node is not None and getattr(node, "adapter", None) is not None:
        robots = [adapter_ref(node)]

    policies = [
        {"type": t, "class": c, "module": m, "config_items": policy_config_items(t)} for t, c, m in policy_adapters()
    ]

    return {"robots": robots, "policies": policies}


def build_router(services: Services) -> APIRouter:
    """构建 health / adapters 路由。"""
    router = APIRouter()
    node = services.node
    identity = services.identity
    leases = services.leases

    @router.get("/v1/health")
    def health():
        """探活：版本 / identity / 已绑定适配器 / 磁盘 / 时钟。

        已绑定适配器只读 node 内存状态（node 主循环已周期 discover 并绑定），
        **不实时 discover**，避免前端轮询本端点时持续对 SDK 进程发 /v1/discover。
        """
        disk = {}
        try:
            usage = shutil.disk_usage("/")
            disk = {"total": usage.total, "used": usage.used, "free": usage.free}
        except OSError:
            disk = {"error": "unavailable"}
        return {
            "status": "ok",
            "version": get_package_version(),
            "identity": identity.headers(),
            "robot": adapter_ref(node),
            "adapters": _adapters(node),
            "disk": disk,
            "time": datetime.now(BEIJING_TZ).isoformat(),
        }

    @router.get("/v1/adapters")
    def adapters_info():
        """Edge 包内**全部已注册**适配器（静态列表，不 discover / 不探活），供 Console 查看。

        与 discover 无关：SDK 进程未启动也应列出全部注册适配器（缺失 SDK / 导入失败
        的跳过）。探活职责归节点（IDLE 探测 / READY 心跳），此处只列静态身份与能力。
        """
        from motrix_edge.adapter import adapter_details

        return {"adapters": adapter_details()}

    @router.get("/v1/adapters/config")
    def adapters_config():
        """运行时 adapter 能力配置（enabled_arms / enabled_cameras）。

        由 ``adapter config`` 命令 / 前端设置，adapter discover 绑定时应用；此处只读。
        """
        if node is None:
            return {}
        return node.adapter_config

    @router.get("/v1/adapters/current")
    def adapters_current():
        """当前绑定 adapter **实际生效**的能力配置（启用的臂 / 相机 / 动作维度 / home）。

        只读（无需租约）：读 adapter 实例实际生效值（``configure()`` 应用后），与
        ``GET /v1/adapters/config``（运行时配置状态）区分。**未绑定 adapter → 404**
        （能力布局是 adapter 类常量、discover 不传，未绑定就没有机型信息，不猜默认）。
        """
        if node is None:
            raise ServiceError("node not initialized", code=ErrorCode.NOT_IMPLEMENTED)
        cfg = node.adapter_config_effective()
        if cfg is None:
            raise ServiceError("adapter not bound", code=ErrorCode.NOT_FOUND)
        return cfg

    @router.post("/v1/adapters/config")
    def adapters_config_set(req: AdapterConfigRequest, x_lease_id: str | None = Header(default=None)):
        """设置运行时 adapter 能力配置（可部分更新；应用到当前已绑定 adapter）。

        受控操作：须持有有效租约（X-Lease-Id）。非法配置 → 400（状态不更新）；
        **会话进行中（采集 / 推理）→ 409**：布局在 episode 中途变化会让同一 episode 的
        qpos / action 维度不一致（mcap 下游按固定维度解析），先退出会话再改。
        """
        if node is None:
            raise ServiceError("node not initialized", code=ErrorCode.NOT_IMPLEMENTED)
        leases.require(x_lease_id)
        if node.session is not None:
            raise ServiceError(
                "adapter config rejected (session active: quit the session first)",
                code=ErrorCode.CONFLICT,
            )
        applied = node.apply_adapter_config(
            {
                "enabled_arms": req.enabled_arms,
                "enabled_cameras": req.enabled_cameras,
            }
        )
        if not applied:
            raise ServiceError("adapter config rejected (invalid arms/cameras)", code=ErrorCode.INVALID_ARGUMENT)
        return node.adapter_config

    return router
