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

"""routes.uploads —— ``/v1/uploads/*`` 的 HTTP 映射（本地 episode 扫描 / 选择 / 打包 / 队列）。

受控操作（与 captures 同规则）：**全部端点须持有效租约**（``X-Lease-Id``）——pack 会
移动文件、scan 会读目录内容，均为敏感操作。扫描目录限定在「数据目录」白名单内
（adapter 上报的采集目录 / ``upload.data_dir``）。

重 IO（算哈希 / 搬文件）→ handler 一律同步 ``def``（FastAPI 交给线程池），见 app.py 的
「路由总则」。
"""

from fastapi import APIRouter, Header

from motrix_edge.server.deps import Services
from motrix_edge.server.schemas import UploadPackRequest, UploadScanRequest, UploadSelectRequest


def build_router(services: Services) -> APIRouter:
    """构建上传 / 扫描路由，并向 UploadSession 注册扫描目录白名单回调。"""
    router = APIRouter()
    node = services.node
    uploads = services.uploads
    leases = services.leases

    def _ensure_upload_lease(lease_id: str | None) -> None:
        """上传端点统一租约校验（语义与 captures 一致：缺失 409 / 不匹配 403 / 过期 410）。

        ``LeaseError`` 直接上抛 → 由 app 的统一错误处理器转 HTTP。
        """
        leases.require(lease_id)

    def _upload_allowed_roots() -> list[str]:
        """允许扫描的目录白名单：adapter 上报的数据目录 + ``upload.data_dir``。"""
        roots: list[str] = []
        if node is not None:
            capture = getattr(node, "capture_status", None)
            data_dir = getattr(capture, "data_dir", None) if capture is not None else None
            if data_dir:
                roots.append(str(data_dir))
        if uploads.default_folder:
            roots.append(str(uploads.default_folder))
        return roots

    uploads.set_allowed_roots(_upload_allowed_roots)

    def _upload_default_folder() -> str | None:
        """缺省扫描目录回退链：请求 ``folder_path`` → **adapter 数据目录** → ``upload.data_dir``。

        adapter 数据目录来自节点缓存的采集状态（``node.capture_status.data_dir``），与前端
        「获取数据目录」按钮（``GET /v1/captures``）**同源**；未绑定 / 无数据目录时回退配置
        目录。
        """
        if node is not None:
            capture = getattr(node, "capture_status", None)
            data_dir = getattr(capture, "data_dir", None) if capture is not None else None
            if data_dir:
                return str(data_dir)
        return uploads.default_folder

    @router.get("/v1/uploads")
    def uploads_status(x_lease_id: str | None = Header(default=None)):
        """当前扫描汇总、episode 状态与选择集（受控：须持租约）。"""
        _ensure_upload_lease(x_lease_id)
        return uploads.status()

    @router.post("/v1/uploads")
    def uploads_scan(req: UploadScanRequest | None = None, x_lease_id: str | None = Header(default=None)):
        """扫描请求目录（缺省回退链：adapter 数据目录 → upload.data_dir）；须持租约。

        目录必须落在允许白名单内（数据目录及其子目录），越界 → 400。
        """
        _ensure_upload_lease(x_lease_id)
        folder_path = req.folder_path if req is not None else None
        return uploads.scan(folder_path or _upload_default_folder())

    @router.post("/v1/uploads/select")
    def uploads_select(req: UploadSelectRequest, x_lease_id: str | None = Header(default=None)):
        """按 episode id 替换待选选择集（受控：须持租约）。"""
        _ensure_upload_lease(x_lease_id)
        return uploads.select(req.episode_ids)

    @router.post("/v1/uploads/pack")
    def uploads_pack(req: UploadPackRequest | None = None, x_lease_id: str | None = Header(default=None)):
        """打包（**移动**）选中 episode 到 ``<扫描目录>/<包名>/``，并返回重扫结果。

        body 可选 ``name``（缺省 ``pack<选中数量>``）；目录同名已存在 → 409（改名后重试）；
        非法包名 → 400；未扫描 / 未选择 → 409；源文件缺失 → 404；移动失败回滚 → 500。
        受控操作（移动数据）：须持租约；同时只允许一个 scan / pack 在跑 → 否则 409。

        回执的 ``scan`` 为收尾重扫结果（前端直接替换列表）；重扫失败时降级为 ``scan=null`` +
        ``warnings``（此时打包**已经成功**，仍回 200）——前端据 ``warnings`` 提示重新扫描。
        """
        _ensure_upload_lease(x_lease_id)
        name = req.name if req is not None else None
        return uploads.pack(name)

    @router.post("/v1/uploads/upload")
    def uploads_enqueue(x_lease_id: str | None = Header(default=None)):
        """把选择集加入上传队列；未配置上传目标时返回 501（须持租约）。"""
        _ensure_upload_lease(x_lease_id)
        return uploads.enqueue()

    @router.post("/v1/uploads/retry")
    def uploads_retry(x_lease_id: str | None = Header(default=None)):
        """把选择集中失败项重置为 pending；实际 uploader 后续实现（须持租约）。"""
        _ensure_upload_lease(x_lease_id)
        return uploads.retry()

    return router
