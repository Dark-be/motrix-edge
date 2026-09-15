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

"""MotrixEdge HTTP API（FastAPI）—— 契约见 wiki/design「后续阶段（M11/M12）」。

当前提供：
  GET /v1/health    版本 / identity / 当前配置选中的适配器（robot / policy）/ 机器人配置 / 磁盘 / 时钟
  POST /v1/commands 命令骨架（accepted）：身份随请求上报，具体执行/校验留待后续
  /v1/captures/*   数据采集会话控制（web 是 node 进程内的独立线程：注入 CaptureService 绑定
                   正在运行的 EdgeNode + 共享 CommandBus，见 wiki/design/motrix_edge_server.md）
  /v1/uploads/*    本地采集 episode 扫描 / 选择 / 打包与上传队列状态（UploadSession，不占
                    RobotAdapter；见 wiki/design/motrix_edge_upload_session.md）
  /v1/infers/*     推理会话控制（无回合概念：enter → 持续推理 → exit；注入 InferService 绑定
                   正在运行的 EdgeNode + 共享 CommandBus）

identity（edge_id / edge_name / edge_version）通过 ``Identity.headers()`` 作为请求元数据上报，
具体发送（访问控制面 / 推理 / 上传）由后续客户端层实现。

信任边界（当前实现）：
  - CORS 全放开（``allow_origins=["*"]``）+ 服务监听 ``0.0.0.0``（node 内嵌 web 线程，见 __main__.py）；
  - 受控操作仅凭 ``X-Lease-Id`` 校验 —— 租约是 Console 前端**自行签发并下发**的本地镜像
    （LeaseManager 只校验），无 TLS / 设备认证 / 服务端身份核验；
  故控制面只适合**可信局域网 / 开发调试**。跨网络 / 生产部署须前置网关做 TLS + 鉴权；
  Console 接入的鉴权（identity 上报核验）落地前，**不要**把 Edge 直接暴露到不受信网络。
"""

import shutil
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from motrix_edge.identity import Identity, load_identity, new_correlation_id
from motrix_edge.lease import BEIJING_TZ, Lease, LeaseError, LeaseManager, LeaseState, build_lease_manager
from motrix_edge.server.capture import CaptureError, CaptureService
from motrix_edge.server.command import CommandError, CommandService
from motrix_edge.server.infer import InferError, InferService
from motrix_edge.server.webrtc import WebRTCError, WebRTCService
from motrix_edge.session.upload_session import UploadError, UploadSession
from motrix_edge.utils.version import get_package_version


def _default_robot(node) -> dict:
    """node 当前绑定的唯一机器人身份（name / type）；未绑定 → None。

    只读 node 内存状态（node 主循环已周期 discover 并绑定 adapter），**不实时
    discover**——避免前端轮询 /v1/health 时持续对 SDK 进程发 /v1/discover。
    """
    adapter = getattr(node, "adapter", None) if node is not None else None
    if adapter is None:
        return {"name": None, "type": None}
    return {
        "name": getattr(node, "adapter_name", None) or getattr(adapter, "name", None),
        "type": getattr(node, "adapter_type", None) or getattr(adapter, "type", None),
    }


def _adapters(node) -> dict:
    """Edge 适配器列表（health 展示 / 客户端选择）。

    - robots：node 当前绑定的唯一机器人（``[{name, type}]``，单 adapter 包；只读
      node 内存状态，**不实时 discover**）。
    - policies：全部已注册策略适配器（``[{type, class, module}]``，前端策略选择用；
      不触发第三方包导入）。
    """
    from motrix_edge.policy import policy_adapters

    robots = []
    adapter = getattr(node, "adapter", None) if node is not None else None
    if adapter is not None:
        robots = [
            {
                "name": getattr(node, "adapter_name", None) or getattr(adapter, "name", None),
                "type": getattr(node, "adapter_type", None) or getattr(adapter, "type", None),
            }
        ]

    policies = [{"type": t, "class": c, "module": m} for t, c, m in policy_adapters()]

    return {"robots": robots, "policies": policies}


class CommandRequest(BaseModel):
    """POST /v1/commands 请求体（契约：lease_id / command_id / capability / 参数 / 现场在场）。

    ``idempotency_key``：**预留、未实现** —— 幂等去重尚未落地（见 server/command.py），
    字段仅作调用方关联回显；调用方需自行处理重试，勿依赖去重。
    """

    command_id: str = Field(..., description="Edge 侧命令 ID")
    lease_id: str | None = None
    capability: str | None = None
    params: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, description="预留：幂等未实现，仅回显关联")
    onsite_presence_context: str | None = None


class CommandResponse(BaseModel):
    command_id: str
    status: str  # accepted / error
    idempotency_key: str | None
    correlation_id: str
    # 命令执行结果（CommandService 返回透传；push 型命令为 None）：
    # executed=执行的 capability / error=执行错误（如 robot execute 维度不符）
    executed: str | None = None
    error: str | None = None
    data: dict | None = None


class LeaseInstallRequest(BaseModel):
    """POST /v1/leases 请求体：Console 签发并下发的租约**镜像**（权威在 Console）。

    字段见 wiki/design/motrix_edge_lease.md「lease 信息」：lease_id / edge_id /
    holder_subject_id / purpose / state / expires_at / renewed_at / lease_version；
    ``ttl`` 为有效期（秒，信息字段）。
    """

    lease_id: str = Field(..., description="Console 生成的租约 id")
    edge_id: str = Field(..., description="租约所属 edge 设备")
    holder_subject_id: str = Field(..., description="租约所属操作员")
    purpose: str = Field(..., description="租约用途（如 capture / rollout / maintenance）")
    state: LeaseState = Field(default=LeaseState.ACTIVE, description="签发状态（reserved / active）")
    expires_at: datetime = Field(..., description="过期时间（ISO 8601，北京时间）")
    lease_version: int = Field(default=1, ge=1, description="租约版本；续约时递增")
    ttl: float | None = Field(default=None, gt=0, description="有效期（秒，信息字段）")


class LeaseRenewRequest(BaseModel):
    """POST /v1/leases/{id}:renew 请求体：Console 续约 —— 更高 lease_version + 新 expires_at。"""

    lease_version: int = Field(..., ge=1, description="新租约版本（须高于当前，版本回退拒绝）")
    expires_at: datetime = Field(..., description="续约后的过期时间（ISO 8601，北京时间）")


class WebRTCOfferRequest(BaseModel):
    """POST /v1/webrtc/offer 请求体：网页 SDP offer。"""

    sdp: str = Field(..., description="网页端 SDP offer")
    type: str = Field(default="offer", description="SDP 类型（offer）")


class InferEnterRequest(BaseModel):
    """POST /v1/infers 请求体：可选推理策略类型（缺省用配置 policy.type）。"""

    policy_type: str | None = Field(default=None, description="推理策略类型（注册表键），如 openpi")


class UploadScanRequest(BaseModel):
    """POST /v1/uploads 请求体：可覆盖配置的默认采集目录。"""

    folder_path: str | None = Field(default=None, description="待扫描目录；缺省使用 upload.data_dir")


class UploadSelectRequest(BaseModel):
    """POST /v1/uploads/select 请求体：按 episode id 替换选择集。"""

    episode_ids: list[str] = Field(default_factory=list)


class UploadPackRequest(BaseModel):
    """POST /v1/uploads/pack 请求体：打包（移动）选中 episode 的包名。

    包目录建在当前扫描目录下（``<folder_path>/<name>/``）；缺省 ``pack<选中数量>``；
    目录同名已存在 → 409（需改名）；见 wiki/design/motrix_edge_upload_session.md。
    """

    name: str | None = Field(default=None, description="包名（单个目录名）；缺省 pack<选中数量>")


class CaptureSyncRequest(BaseModel):
    """POST /v1/captures/sync 请求体：采集元信息（采集员 / 任务名等，进程保存数据时附加）。"""

    meta: dict = Field(default_factory=dict, description="采集元信息（operator / task_name 等）")


class CaptureMetaAddRequest(BaseModel):
    """POST /v1/captures/meta 请求体：新增采集元信息选项（分类不存在则自动创建）。"""

    key: str = Field(..., description="分类（如 operator / task_name）")
    value: str = Field(..., description="选项值")


class CaptureMetaEditRequest(BaseModel):
    """PATCH /v1/captures/meta 请求体：重命名采集元信息选项（``old`` → ``new``）。"""

    key: str = Field(..., description="分类")
    old: str = Field(..., description="原选项值")
    new: str = Field(..., description="新选项值")


def create_app(
    base_cfg: dict,
    node=None,
    captures: CaptureService | None = None,
    infers: InferService | None = None,
    commands: CommandService | None = None,
    lease_manager: LeaseManager | None = None,
    webrtc: WebRTCService | None = None,
    uploads: UploadSession | None = None,
) -> FastAPI:
    """构建 MotrixEdge FastAPI 应用。base_cfg 加载一次 identity 与 robot 配置。

    node: 可选 ``EdgeNode``（正在运行的节点实例）。注入后 ``/v1/health`` 从 node
          内存状态读已绑定 adapter（**不实时 discover**，避免前端轮询持续发
          /v1/discover）；未注入时 health 的 robot / robots 返回空。
    captures: 可选 ``CaptureService``（绑定正在运行的 EdgeNode + 共享 CommandBus）；
              注入后注册 ``/v1/captures/*`` 数据采集回合接口，未注入时这些端点返回 501。
    infers: 可选 ``InferService``（绑定正在运行的 EdgeNode + 共享 CommandBus）；注入后
              注册 ``/v1/infers/*`` 推理会话接口，未注入时这些端点返回 501。
    commands: 可选 ``CommandService``（受控命令：租约校验 + estop）；未注入时
              ``/v1/commands`` 保持骨架（accepted）。
    lease_manager: 可选 ``LeaseManager``（Edge 级租约，独立于任务）；缺省自建，
                   ``/v1/leases/*`` 总可用。
    webrtc: 可选 ``WebRTCService``（aiortc 推流，视频轨道从 FrameManager 取帧）；
            未注入时 ``/v1/webrtc/offer`` 返回 501。
    uploads: 可选 ``UploadSession``；缺省按 ``base_cfg.upload`` 创建，用于本地 episode
             扫描与选择（见``/v1/uploads/*``）。
    """
    identity: Identity = load_identity(base_cfg)
    # 租约配置（``lease`` 段）：ttl = 租约有效期，renew_interval = 建议续租间隔
    lease_manager = lease_manager or build_lease_manager(base_cfg)
    # 上传会话（``upload`` 段）：本地 episode 扫描 / 选择 / 打包与上传队列状态
    uploads = uploads or UploadSession(base_cfg)

    app = FastAPI(title="MotrixEdge", version=get_package_version())

    # 浏览器 viewer（file:// 或任意端口打开）跨源访问：开发/调试期放开 CORS。
    # **信任边界**：配合服务监听 0.0.0.0（__main__.py），控制面无 TLS / 鉴权（仅
    # X-Lease-Id 本地租约镜像，前端自签），只适合可信局域网 / 调试；对外暴露须前置
    # 网关 TLS + 鉴权（详见本模块 docstring「信任边界」）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,  # 用 X-Lease-Id 头，不用 cookie，可通配 origin
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _correlation_middleware(request: Request, call_next):
        corr = request.headers.get("X-Correlation-Id") or new_correlation_id()
        request.state.correlation_id = corr
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = corr
        return response

    # ---- 路由总则：所有 HTTP handler 一律同步 ``def``（FastAPI 交给线程池）--------
    #
    # 这些 handler 内部都是**阻塞调用**：``CommandBus.submit`` 同步等回执（最长 5s）、
    # 磁盘 / 文件操作（scan 算 SHA-256、pack 搬文件）、adapter 的同步 HTTP 查询。写成
    # ``async def`` 会占住 uvicorn 事件循环，连带冻结 health / preview / WebRTC 信令。
    # 唯一例外是本文件顶部的 correlation 中间件（必须 ``async def``）。

    @app.get("/v1/health")
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
            "robot": _default_robot(node),
            "adapters": _adapters(node),
            "disk": disk,
            "time": datetime.now(BEIJING_TZ).isoformat(),
        }

    @app.get("/v1/adapters")
    def adapters_info():
        """Edge 包内**全部已注册**适配器（静态列表，不 discover / 不探活），供 Console 查看。

        与 discover 无关：SDK 进程未启动也应列出全部注册适配器（缺失 SDK / 导入失败
        的跳过）。探活职责归节点（IDLE 探测 / READY 心跳），此处只列静态身份与能力。
        """
        from motrix_edge.adapter import adapter_details

        return {"adapters": adapter_details()}

    @app.post("/v1/webrtc/offer")
    def webrtc_offer(req: WebRTCOfferRequest, x_lease_id: str | None = Header(default=None)):
        """WebRTC 推流：接收网页 SDP offer，返回 Edge answer。受控操作：须持有有效租约。"""
        if webrtc is None:
            raise HTTPException(status_code=501, detail="webrtc not enabled")
        try:
            return webrtc.offer(lease_id=x_lease_id, sdp=req.sdp, sdp_type=req.type)
        except WebRTCError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/v1/commands")
    def command(req: CommandRequest, request: Request):
        """受控命令：须持有有效租约（``lease_id``）；``capability=estop`` → 全局急停。

        未注入 CommandService 时保持骨架（accepted）；具体执行 / Capability 校验留待后续。
        """
        corr = getattr(request.state, "correlation_id", None) or new_correlation_id()
        executed = None
        error = None
        data = None
        if commands is not None:
            try:
                result = commands.execute(
                    command_id=req.command_id,
                    lease_id=req.lease_id,
                    capability=req.capability,
                    params=req.params,
                )
                executed = result.get("executed")
                error = result.get("error")
                data = result.get("data")
            except CommandError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return CommandResponse(
            command_id=req.command_id,
            status="error" if error else "accepted",
            idempotency_key=req.idempotency_key,
            correlation_id=corr,
            executed=executed,
            error=error,
            data=data,
        )

    # ---- /v1/leases/*：Edge 级租约（独立于机器人 / 任务；受控操作须持有）----

    def _leases_call(fn):
        try:
            return fn()
        except LeaseError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/v1/leases")
    def leases_install(req: LeaseInstallRequest):
        """Console 生成租约并下发，Edge 接收保存本地**镜像**（Console 权威）。

        Edge 不生成 lease_id，只保留 + 校验；已有活跃控制租约 → 409。
        """
        _leases_call(
            lambda: lease_manager.install(
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
        )
        return {"status": "accepted", **lease_manager.status()}

    @app.post("/v1/leases/{lease_id}:renew")
    def leases_renew(lease_id: str, req: LeaseRenewRequest):
        """Console 续约：lease_version 递增（版本回退拒绝），Edge 更新本地镜像。

        续约 = 以更高 ``lease_version`` 原地延长 ``expires_at``；Edge 在旧租约到期前
        收到新镜像即可保持控制。
        """
        lease = _leases_call(lambda: lease_manager.renew(lease_id, req.lease_version, req.expires_at))
        return {
            "status": "accepted",
            "lease_id": lease.lease_id,
            "lease_version": lease.lease_version,
            "state": lease.state.value,
            "expires_at": lease.expires_at.isoformat(),
        }

    @app.get("/v1/leases/{lease_id}")
    def leases_get(lease_id: str):
        """查询 Edge 本地 lease 镜像状态：``200``（返回 lease 信息）/ ``404``（不存在）。"""
        return _leases_call(lambda: lease_manager.mirror(lease_id))

    @app.post("/v1/leases/{lease_id}:revoke")
    def leases_revoke(lease_id: str):
        """Console 撤销租约：Edge 进入无效状态（Revoked），不能执行受限操作。"""
        lease = _leases_call(lambda: lease_manager.revoke(lease_id))
        return {"status": "accepted", "lease_id": lease.lease_id, "state": lease.state.value}

    @app.get("/v1/leases")
    def leases_status():
        """租约状态汇总（只读，Edge 侧）：当前租约 / leasable / renew_interval。"""
        return lease_manager.status()

    # ---- /v1/uploads/*：本地采集 episode 扫描、选择与上传队列 --------------------
    #
    # 受控操作（与 captures 同规则）：**全部端点须持有效租约**（``X-Lease-Id``）——pack 会
    # 移动文件、scan 会读目录内容，均为敏感操作。
    # 扫描目录限定在「数据目录」白名单内（adapter 上报的采集目录 / ``upload.data_dir``）。
    # 重 IO（算哈希 / 搬文件）→ handler 必须是同步 ``def``，见上面「路由总则」。

    def _ensure_upload_lease(lease_id: str | None) -> None:
        """上传端点统一租约校验（语义与 captures 一致：缺失 409 / 不匹配 403 / 过期 410）。"""
        try:
            lease_manager.require(lease_id)
        except LeaseError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

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

    def _upload_call(fn):
        try:
            return fn()
        except UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

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

    @app.get("/v1/uploads")
    def uploads_status(x_lease_id: str | None = Header(default=None)):
        """当前扫描汇总、episode 状态与选择集（受控：须持租约）。"""
        _ensure_upload_lease(x_lease_id)
        return uploads.status()

    @app.post("/v1/uploads")
    def uploads_scan(req: UploadScanRequest | None = None, x_lease_id: str | None = Header(default=None)):
        """扫描请求目录（缺省回退链：adapter 数据目录 → upload.data_dir）；须持租约。

        目录必须落在允许白名单内（数据目录及其子目录），越界 → 400。
        """
        _ensure_upload_lease(x_lease_id)
        folder_path = req.folder_path if req is not None else None
        return _upload_call(lambda: uploads.scan(folder_path or _upload_default_folder()))

    @app.post("/v1/uploads/select")
    def uploads_select(req: UploadSelectRequest, x_lease_id: str | None = Header(default=None)):
        """按 episode id 替换待选选择集（受控：须持租约）。"""
        _ensure_upload_lease(x_lease_id)
        return _upload_call(lambda: uploads.select(req.episode_ids))

    @app.post("/v1/uploads/pack")
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
        return _upload_call(lambda: uploads.pack(name))

    @app.post("/v1/uploads/upload")
    def uploads_enqueue(x_lease_id: str | None = Header(default=None)):
        """把选择集加入上传队列；未配置上传目标时返回 501（须持租约）。"""
        _ensure_upload_lease(x_lease_id)
        return _upload_call(uploads.enqueue)

    @app.post("/v1/uploads/retry")
    def uploads_retry(x_lease_id: str | None = Header(default=None)):
        """把选择集中失败项重置为 pending；实际 uploader 后续实现（须持租约）。"""
        _ensure_upload_lease(x_lease_id)
        return _upload_call(uploads.retry)

    # ---- /v1/captures/*：数据采集回合控制（web 线程 → CaptureService → CommandBus）----

    def _captures():
        if captures is None:
            raise HTTPException(
                status_code=501,
                detail="captures not enabled (run 'motrix-edge serve' with capture service)",
            )
        return captures

    def _capture_call(fn):
        try:
            return fn()
        except CaptureError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/v1/captures")
    def captures_status():
        """状态快照：state / adapter / data_dir / disk / capture_running。"""
        return _captures().status()

    @app.get("/v1/captures/precheck")
    def captures_precheck():
        """预检（只读）：机器人就绪 + 磁盘 + 当前租约 / 可租状态。"""
        return _captures().precheck()

    @app.get("/v1/preview")
    def captures_preview(x_lease_id: str | None = Header(default=None)):
        """最新观测预览：qpos / action 状态 + 摄像头名列表（图像走 WebRTC，不内联）。

        受控操作：须持有有效租约（X-Lease-Id）。
        """
        return _capture_call(lambda: _captures().preview(lease_id=x_lease_id))

    @app.get("/v1/captures/meta")
    def captures_meta():
        """采集元信息选项（config/capture.yml 的 ``meta`` 段；前端选择列表用，只读）。"""
        return _capture_call(lambda: _captures().meta())

    @app.post("/v1/captures/meta")
    def captures_meta_add(req: CaptureMetaAddRequest, x_lease_id: str | None = Header(default=None)):
        """新增采集元信息选项（分类不存在则自动创建）；重复 → 400。须持租约。

        回执与 ``GET /v1/captures/meta`` 同构（``{meta: 全量}``），前端写后无需再拉取。
        选项管理是**配置级**操作，与机器人进程 / 会话状态无关（不进状态机）。
        """
        return _capture_call(lambda: _captures().meta_add(req.key, req.value, lease_id=x_lease_id))

    @app.patch("/v1/captures/meta")
    def captures_meta_edit(req: CaptureMetaEditRequest, x_lease_id: str | None = Header(default=None)):
        """重命名采集元信息选项（``key`` 下 ``old`` → ``new``）；不存在 / 重复 → 400。须持租约。"""
        return _capture_call(lambda: _captures().meta_edit(req.key, req.old, req.new, lease_id=x_lease_id))

    @app.delete("/v1/captures/meta")
    def captures_meta_delete(key: str, value: str, x_lease_id: str | None = Header(default=None)):
        """删除采集元信息选项（分类清空则一并删除该分类）；不存在 → 400。须持租约。

        ``key`` / ``value`` 经 **query 参数**提交（选项值可能含空格 / 中文）。
        删除整个分类见 ``DELETE /v1/captures/meta/{key}``。
        """
        return _capture_call(lambda: _captures().meta_delete(key, value, lease_id=x_lease_id))

    @app.delete("/v1/captures/meta/{key}")
    def captures_meta_delete_key(key: str, x_lease_id: str | None = Header(default=None)):
        """删除整个采集元信息分类；分类不存在 → 400。须持租约。"""
        return _capture_call(lambda: _captures().meta_delete_key(key, lease_id=x_lease_id))

    @app.post("/v1/captures/sync")
    def captures_sync(req: CaptureSyncRequest, x_lease_id: str | None = Header(default=None)):
        """同步采集元信息（采集员 / 任务名等）到机器人进程：进程保存一轮数据时附加。

        受控操作：须持有有效租约（X-Lease-Id）；采集会话内消费。
        """
        return _capture_call(lambda: _captures().sync(req.meta, lease_id=x_lease_id))

    @app.post("/v1/captures")
    def captures_enter(x_lease_id: str | None = Header(default=None)):
        """创建采集会话（进入任务环境）：READY → ACTIVE，需先持有有效租约（X-Lease-Id）。

        单 adapter 包：无 adapter 选择，采集基于节点绑定的唯一 adapter；已在环境中 → 409。
        采集为观测会话（无回合流程控制）：进入后持续读共享内存观测，session quit 退出。
        """
        return _capture_call(lambda: _captures().enter(lease_id=x_lease_id))

    @app.delete("/v1/captures")
    def captures_exit(lease_id: str | None = None):
        """退出采集任务环境（ACTIVE → IDLE）。租约经 query 参数 `lease_id` 提交校验。

        租约不随退出销毁 —— 生命周期由 Edge 级 `/v1/leases/*`（activate / renew / release /
        revoke）管理，session 只消费（校验）租约。
        """
        return _capture_call(lambda: _captures().exit(lease_id=lease_id))

    # ---- /v1/infers/*：推理会话控制（无回合概念：enter → 持续推理 → exit）----

    def _infers():
        if infers is None:
            raise HTTPException(
                status_code=501,
                detail="infers not enabled (run 'motrix-edge serve' with infer service)",
            )
        return infers

    def _infer_call(fn):
        try:
            return fn()
        except InferError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/v1/infers")
    def infers_status():
        """状态快照：node_state / adapter / policy / running / lease_id。"""
        return _infers().status()

    @app.post("/v1/infers")
    def infers_enter(req: InferEnterRequest | None = None, x_lease_id: str | None = Header(default=None)):
        """进入推理会话（READY → ACTIVE）：连接推理会话并启动任务循环，需先持有有效租约。

        请求体可选：``policy_type`` 指定推理策略（缺省用配置 policy.type）。
        """
        policy_type = req.policy_type if req is not None else None
        return _infer_call(lambda: _infers().enter(lease_id=x_lease_id, policy_type=policy_type))

    @app.post("/v1/infers/rollout")
    def infers_rollout(x_lease_id: str | None = Header(default=None)):
        """单步推理闭环（infer rollout）：上传观测 → 推理 → 下发动作。须已在推理会话且持有租约。"""
        return _infer_call(lambda: _infers().rollout(lease_id=x_lease_id))

    @app.delete("/v1/infers")
    def infers_exit(lease_id: str | None = None):
        """退出推理会话（ACTIVE → READY）。租约经 query 参数 `lease_id` 提交校验。"""
        return _infer_call(lambda: _infers().exit(lease_id=lease_id))

    return app
