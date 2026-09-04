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
  /v1/infers/*     推理会话控制（无回合概念：enter → 持续推理 → exit；注入 InferService 绑定
                   正在运行的 EdgeNode + 共享 CommandBus）
  /v1/uploads/*    本地采集 episode 扫描、选择与上传队列

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
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from motrix_edge.identity import Identity, load_identity, new_correlation_id
from motrix_edge.lease import BEIJING_TZ, Lease, LeaseError, LeaseManager, LeaseState, build_lease_manager
from motrix_edge.server.capture import CaptureError, CaptureService
from motrix_edge.server.command import CommandError, CommandService
from motrix_edge.server.infer import InferError, InferService
from motrix_edge.server.preview import PreviewError, PreviewService
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
    holder_subject_id / purpose / state / lease_version；``ttl`` 为有效期（秒）。
    ``expires_at`` 可选：**Edge 权威时钟计算** ``now + ttl`` 后覆盖，仅向前兼容保留。
    """

    lease_id: str = Field(..., description="Console 生成的租约 id")
    edge_id: str = Field(..., description="租约所属 edge 设备")
    holder_subject_id: str = Field(..., description="租约所属操作员")
    purpose: str = Field(..., description="租约用途（如 capture / rollout / maintenance）")
    state: LeaseState = Field(default=LeaseState.ACTIVE, description="签发状态（reserved / active）")
    expires_at: datetime | None = Field(default=None, description="过期时间（忽略；Edge 按 now+ttl 重算）")
    lease_version: int = Field(default=1, ge=1, description="租约版本；续约时递增")
    ttl: float | None = Field(default=None, gt=0, description="有效期（秒）—— Edge 据此计算 expires_at")


class LeaseRenewRequest(BaseModel):
    """POST /v1/leases/{id}:renew 请求体：Console 续约 —— 更高 lease_version（可选 ttl）。

    ``expires_at`` 由 Edge 权威时钟按 ``now + ttl`` 计算（不再由客户端提供）。
    """

    lease_version: int = Field(..., ge=1, description="新租约版本（须高于当前，版本回退拒绝）")
    ttl: float | None = Field(default=None, gt=0, description="续约有效期（秒）；缺省沿用当前租约 ttl")


class WebRTCOfferRequest(BaseModel):
    """POST /v1/webrtc/offer 请求体：网页 SDP offer。"""

    sdp: str = Field(..., description="网页端 SDP offer")
    type: str = Field(default="offer", description="SDP 类型（offer）")


class InferEnterRequest(BaseModel):
    """POST /v1/infers 请求体：可选推理策略类型（缺省用配置 policy.type）。"""

    policy_type: str | None = Field(default=None, description="推理策略类型（注册表键），如 openpi")


class InferRolloutRequest(BaseModel):
    """POST /v1/infers/rollout 请求体：推理模式 + 步数（count 模式）。"""

    mode: Literal["count", "continuous", "drain"] | None = Field(
        default=None, description="推理模式：count（缺省）/ continuous / drain"
    )
    count: int | None = Field(default=None, ge=1, le=100, description="连续推理步数（count 模式，缺省 1）")


class UploadScanRequest(BaseModel):
    """POST /v1/uploads 请求体：可覆盖配置的默认采集目录。"""

    folder_path: str | None = Field(default=None, description="待扫描目录；缺省使用 upload.data_dir")


class CaptureSyncRequest(BaseModel):
    """POST /v1/captures/sync 请求体：采集元信息（采集员 / 任务名等，进程保存数据时附加）。"""

    meta: dict = Field(default_factory=dict, description="采集元信息（operator / task_name 等）")


class AdapterConfigRequest(BaseModel):
    """POST /v1/adapters/config 请求体：运行时 adapter 能力配置（可部分更新）。"""

    enabled_arms: list[str] | None = Field(default=None, description="启用的机械臂（right / left）；缺省全部")
    enabled_cameras: list[str] | None = Field(default=None, description="启用的相机（IMAGES 子集）")


class UploadSelectRequest(BaseModel):
    """POST /v1/uploads/select 请求体：按 episode id 替换选择集。"""

    episode_ids: list[str] = Field(default_factory=list)


def create_app(
    base_cfg: dict,
    node=None,
    captures: CaptureService | None = None,
    infers: InferService | None = None,
    commands: CommandService | None = None,
    lease_manager: LeaseManager | None = None,
    webrtc: WebRTCService | None = None,
    uploads: UploadSession | None = None,
    preview: PreviewService | None = None,
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
    uploads: 可选 ``UploadSession``；缺省按 ``base_cfg.upload`` 创建，用于本地 episode 扫描与选择。
    preview: 可选 ``PreviewService``（**独立于采集 / 推理会话**，直接读 node.frame_manager
             观测缓存）；注入后注册 ``/v1/preview`` 观测预览端点，未注入时返回 501。
    """
    identity: Identity = load_identity(base_cfg)
    # 租约配置（``lease`` 段）：ttl = 租约有效期，renew_interval = 建议续租间隔
    lease_manager = lease_manager or build_lease_manager(base_cfg)
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

    @app.middleware("http")
    async def _no_store_cache(request: Request, call_next):
        """控制面（/v1/*）响应一律 ``Cache-Control: no-store``：实时状态禁止浏览器缓存。

        预览 / 租约等轮询 GET 若被浏览器缓存，会回放旧的 410 / 过期状态（同一 URL
        每秒轮询命中缓存，表现为 "date" 是旧时间、请求不进服务端日志）。
        """
        response = await call_next(request)
        if request.url.path.startswith("/v1"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/v1/health")
    async def health():
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
    async def adapters_info():
        """Edge 包内**全部已注册**适配器（静态列表，不 discover / 不探活），供 Console 查看。

        与 discover 无关：SDK 进程未启动也应列出全部注册适配器（缺失 SDK / 导入失败
        的跳过）。探活职责归节点（IDLE 探测 / READY 心跳），此处只列静态身份与能力。
        """
        from motrix_edge.adapter import adapter_details

        return {"adapters": adapter_details()}

    @app.get("/v1/adapters/config")
    async def adapters_config():
        """运行时 adapter 能力配置（enabled_arms / enabled_cameras）。

        由 ``adapter config`` 命令 / 前端设置，adapter discover 绑定时应用；此处只读。
        """
        if node is None:
            return {}
        return node.adapter_config

    @app.get("/v1/adapters/current")
    async def adapters_current():
        """当前绑定 adapter **实际生效**的能力配置（启用的臂 / 相机 / 动作维度 / home）。

        只读（无需租约）：读 adapter 实例实际生效值（``configure()`` 应用后），与
        ``GET /v1/adapters/config``（运行时配置状态）区分。**未绑定 adapter → 回退包内
        默认 adapter 的默认配置**（``default=True``，前端刷新即可见勾选）；无任何注册
        adapter → 404。
        """
        if node is None:
            raise HTTPException(status_code=501, detail="node not initialized")
        cfg = node.adapter_config_effective()
        if cfg is None:
            raise HTTPException(status_code=404, detail="no adapter registered")
        return cfg

    @app.post("/v1/adapters/config")
    async def adapters_config_set(req: AdapterConfigRequest, x_lease_id: str | None = Header(default=None)):
        """设置运行时 adapter 能力配置（可部分更新；应用到当前已绑定 adapter）。

        受控操作：须持有有效租约（X-Lease-Id）。非法配置 → 400（状态不更新）。
        """
        if node is None:
            raise HTTPException(status_code=501, detail="node not initialized")
        try:
            lease_manager.require(x_lease_id)
        except LeaseError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        applied = node._apply_adapter_config(
            {
                "enabled_arms": req.enabled_arms,
                "enabled_cameras": req.enabled_cameras,
            }
        )
        if not applied:
            raise HTTPException(status_code=400, detail="adapter config rejected (invalid arms/cameras)")
        return node.adapter_config

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
    async def command(req: CommandRequest, request: Request):
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
    async def leases_install(req: LeaseInstallRequest):
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
    async def leases_renew(lease_id: str, req: LeaseRenewRequest):
        """Console 续约：lease_version 递增（版本回退拒绝），Edge 更新本地镜像。

        续约 = 以更高 ``lease_version`` 原地延长 ``expires_at``；Edge 在旧租约到期前
        收到新镜像即可保持控制。
        """
        lease = _leases_call(lambda: lease_manager.renew(lease_id, req.lease_version, ttl=req.ttl))
        return {
            "status": "accepted",
            "lease_id": lease.lease_id,
            "lease_version": lease.lease_version,
            "state": lease.state.value,
            "expires_at": lease.expires_at.astimezone(BEIJING_TZ).isoformat(),
        }

    @app.get("/v1/leases/{lease_id}")
    async def leases_get(lease_id: str):
        """查询 Edge 本地 lease 镜像状态：``200``（返回 lease 信息）/ ``404``（不存在）。"""
        return _leases_call(lambda: lease_manager.mirror(lease_id))

    @app.post("/v1/leases/{lease_id}:revoke")
    async def leases_revoke(lease_id: str):
        """Console 撤销租约：Edge 进入无效状态（Revoked），不能执行受限操作。"""
        lease = _leases_call(lambda: lease_manager.revoke(lease_id))
        return {"status": "accepted", "lease_id": lease.lease_id, "state": lease.state.value}

    @app.get("/v1/leases")
    async def leases_status():
        """租约状态汇总（只读，Edge 侧）：当前租约 / leasable / renew_interval。"""
        return lease_manager.status()

    # ---- /v1/uploads/*：本地采集 episode 扫描、选择与上传队列 -----------------

    def _upload_call(fn):
        try:
            return fn()
        except UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    def _upload_default_folder() -> str | None:
        """缺省扫描目录回退链：请求 ``folder_path`` → **adapter 数据目录** → ``upload.data_dir``。

        adapter 数据目录来自节点缓存的采集数据状态（``node.data_status.data_dir``），
        与前端「获取数据目录」按钮（``GET /v1/captures``）**同源**；未绑定 / 无数据
        目录时回退配置目录。
        """
        if node is not None:
            data_status = node.data_status
            data_dir = getattr(data_status, "data_dir", None) if data_status is not None else None
            if data_dir:
                return data_dir
        return uploads.default_folder

    @app.get("/v1/uploads")
    async def uploads_status():
        """当前扫描汇总、episode 状态与选择集。"""
        return uploads.status()

    @app.post("/v1/uploads")
    async def uploads_scan(req: UploadScanRequest | None = None):
        """扫描请求目录；缺省回退链：adapter 数据目录 → upload.data_dir。"""
        folder_path = req.folder_path if req is not None else None
        return _upload_call(lambda: uploads.scan(folder_path or _upload_default_folder()))

    @app.post("/v1/uploads/select")
    async def uploads_select(req: UploadSelectRequest):
        """按 episode id 替换待上传选择集。"""
        return _upload_call(lambda: uploads.select(req.episode_ids))

    @app.post("/v1/uploads/upload")
    async def uploads_enqueue():
        """把选择集加入上传队列；未配置上传目标时返回 501。"""
        return _upload_call(uploads.enqueue)

    @app.post("/v1/uploads/retry")
    async def uploads_retry():
        """把选择集中失败项重置为 pending；实际 uploader 后续实现。"""
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
    async def captures_status():
        """状态快照：state / adapter / data_dir / data_files / disk / running。"""
        return _captures().status()

    @app.get("/v1/captures/precheck")
    async def captures_precheck():
        """预检（只读）：机器人就绪 + 磁盘 + 当前租约 / 可租状态。"""
        return _captures().precheck()

    @app.get("/v1/preview")
    async def preview_endpoint(x_lease_id: str | None = Header(default=None)):
        """最新观测预览：qpos / action 状态 + 摄像头名列表（图像走 WebRTC，不内联）。

        独立于采集 / 推理会话（PreviewService 直接读 node.frame_manager 观测缓存）：
        不要求会话，预览随时可开；受控操作：须持有有效租约（X-Lease-Id）。
        """
        if preview is None:
            raise HTTPException(status_code=501, detail="preview not enabled")
        try:
            return preview.preview(lease_id=x_lease_id)
        except PreviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/v1/captures/meta")
    async def captures_meta():
        """采集元信息选项（config/capture.yml 的 ``meta`` 段）：前端选择列表，只读免租约。"""
        return _captures().meta()

    @app.post("/v1/captures")
    async def captures_enter(x_lease_id: str | None = Header(default=None)):
        """创建采集会话（进入任务环境）：READY → ACTIVE，需先持有有效租约（X-Lease-Id）。

        单 adapter 包：无 adapter 选择，采集基于节点绑定的唯一 adapter；已在环境中 → 409。
        采集为观测会话（无回合流程控制）：进入后持续读共享内存观测，session quit 退出。
        """
        return _capture_call(lambda: _captures().enter(lease_id=x_lease_id))

    @app.delete("/v1/captures")
    async def captures_exit(lease_id: str | None = None):
        """退出采集任务环境（ACTIVE → IDLE）。租约经 query 参数 `lease_id` 提交校验。

        租约不随退出销毁 —— 生命周期由 Edge 级 `/v1/leases/*`（activate / renew / release /
        revoke）管理，session 只消费（校验）租约。
        """
        return _capture_call(lambda: _captures().exit(lease_id=lease_id))

    @app.post("/v1/captures/sync")
    async def captures_sync(req: CaptureSyncRequest, x_lease_id: str | None = Header(default=None)):
        """同步采集元信息（采集员 / 任务名等）到机器人进程（进程保存一轮数据时附加）。

        受控操作：须持有有效租约（X-Lease-Id）。
        """
        return _capture_call(lambda: _captures().sync(meta=req.meta, lease_id=x_lease_id))

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
    async def infers_status():
        """状态快照：node_state / adapter / policy / running / lease_id。"""
        return _infers().status()

    @app.post("/v1/infers")
    async def infers_enter(req: InferEnterRequest | None = None, x_lease_id: str | None = Header(default=None)):
        """进入推理会话（READY → ACTIVE）：连接推理会话并启动任务循环，需先持有有效租约。

        请求体可选：``policy_type`` 指定推理策略（缺省用配置 policy.type）。
        """
        policy_type = req.policy_type if req is not None else None
        return _infer_call(lambda: _infers().enter(lease_id=x_lease_id, policy_type=policy_type))

    @app.post("/v1/infers/connect")
    async def infers_connect(x_lease_id: str | None = Header(default=None)):
        """单次尝试连接推理节点（infer connect）。须已在推理会话且持有租约。"""
        return _infer_call(lambda: _infers().connect(lease_id=x_lease_id))

    @app.post("/v1/infers/rollout")
    async def infers_rollout(req: InferRolloutRequest | None = None, x_lease_id: str | None = Header(default=None)):
        """推理闭环（infer rollout [count] / continuous / drain）。

        body：``mode``（count 缺省 / continuous / drain）+ ``count``（count 模式，缺省 1，1–100）。
        须已在推理会话且持有租约；continuous 启动即回执 started，直到 session quit / estop。
        """
        mode = req.mode if req is not None else None
        count = req.count if req is not None else None
        return _infer_call(lambda: _infers().rollout(lease_id=x_lease_id, mode=mode, count=count))

    @app.delete("/v1/infers")
    async def infers_exit(lease_id: str | None = None):
        """退出推理会话（ACTIVE → READY）。租约经 query 参数 `lease_id` 提交校验。"""
        return _infer_call(lambda: _infers().exit(lease_id=lease_id))

    return app
