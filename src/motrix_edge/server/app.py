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

"""MotrixEdge HTTP API（FastAPI）—— 应用装配 + 中间件 + 统一错误处理。

端点按域拆在 ``server/routes/*``（每个模块只做 HTTP 映射：入参 → controller → dict）；
controller 在 ``server/*.py``（域语义，不依赖 FastAPI；分层见 ``server/deps.py``）。
本模块只做四件事：缺省构造 identity / 租约 / 上传会话、挂中间件、注册统一错误处理器、
挂载各域 router。端点清单见 wiki/design/motrix_edge_server.md。

**路由总则**：所有 HTTP handler 一律同步 ``def``（FastAPI 交给线程池）——它们内部都是
**阻塞调用**（``CommandBus.submit`` 同步等回执，最长 5s；磁盘 / 文件操作算哈希、搬文件；
adapter 的同步 HTTP 查询）。写成 ``async def`` 会占住 uvicorn 事件循环，连带冻结
health / preview / WebRTC 信令。仅中间件（correlation / no-store）用 ``async def``）。

**错误处理**：各层只抛 ``ServiceError`` 子类（``motrix_edge.errors``）且只讲 **edge 错误码**
（``ErrorCode``）——命令层 ``CommandError``、租约层 ``LeaseError``、会话层 ``UploadError``、
服务层各 ``*Error``；**HTTP 状态码由本层维护**（``_HTTP_STATUS`` 映射），响应体回传 code：
``{"detail": ..., "code": ...}``。

信任边界（当前实现）：
  - CORS 全放开（``allow_origins=["*"]``）+ 服务监听 ``0.0.0.0``（node 内嵌 web 线程，见 __main__.py）；
  - 受控操作仅凭 ``X-Lease-Id`` 校验 —— 租约是 Console 前端**自行签发并下发**的本地镜像
    （LeaseManager 只校验），无 TLS / 设备认证 / 服务端身份核验；
  故控制面只适合**可信局域网 / 开发调试**。跨网络 / 生产部署须前置网关做 TLS + 鉴权；
  Console 接入的鉴权（identity 上报核验）落地前，**不要**把 Edge 直接暴露到不受信网络。
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.identity import Identity, load_identity, new_correlation_id
from motrix_edge.lease import LeaseManager, build_lease_manager
from motrix_edge.server.command import CommandService
from motrix_edge.server.deps import Services
from motrix_edge.server.meta import CaptureMetaService
from motrix_edge.server.preview import PreviewService
from motrix_edge.server.routes import build_routers
from motrix_edge.server.rpent import RpentService
from motrix_edge.server.webrtc import WebRTCService
from motrix_edge.session.upload_session import UploadSession
from motrix_edge.utils.version import get_package_version

# edge 错误码 → HTTP 状态码（**HTTP 面自行维护**：业务层只给 code，不认状态码）
_HTTP_STATUS: dict[str, int] = {
    ErrorCode.INVALID_ARGUMENT: 400,
    ErrorCode.UNKNOWN_COMMAND: 404,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.LEASE_REQUIRED: 409,
    ErrorCode.LEASE_EXPIRED: 410,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_IMPLEMENTED: 501,
    ErrorCode.UPSTREAM_ERROR: 502,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.INTERNAL: 500,
}


def create_app(
    base_cfg: dict,
    node=None,
    commands: CommandService | None = None,
    lease_manager: LeaseManager | None = None,
    webrtc: WebRTCService | None = None,
    uploads: UploadSession | None = None,
    preview: PreviewService | None = None,
    meta: CaptureMetaService | None = None,
    rpent: RpentService | None = None,
) -> FastAPI:
    """构建 MotrixEdge FastAPI 应用。base_cfg 加载一次 identity 与 robot 配置。

    node: 可选 ``EdgeNode``（正在运行的节点实例）。注入后 ``/v1/health`` 与各状态快照
          （``/v1/captures`` · ``/v1/infers`` · ``precheck``）从 node 内存状态读已绑定
          adapter（**不实时 discover**，避免前端轮询持续发 /v1/discover）；未注入时读端点
          返回 501。
    commands: 可选 ``CommandService``（**唯一写通道**：受控命令 + 全部会话动作）；
              未注入时写端点返回 501。
    lease_manager: 可选 ``LeaseManager``（Edge 级租约，独立于任务）；缺省自建，
                   ``/v1/leases/*`` 总可用。
    webrtc: 可选 ``WebRTCService``（aiortc 推流，视频轨道从 FrameManager 取帧）；
            未注入时 ``/v1/webrtc/offer`` 返回 501。
    uploads: 可选 ``UploadSession``；缺省按 ``base_cfg.upload`` 创建，用于本地 episode 扫描与选择。
    preview: 可选 ``PreviewService``（**独立于采集 / 推理会话**，直接读 node.frame_manager
             观测缓存）；注入后注册 ``/v1/preview`` 观测预览端点，未注入时返回 501。
    meta: 可选 ``CaptureMetaService``（采集元信息选项，直连 ``CaptureMetaStore``）；
          未注入时 ``/v1/captures/meta*`` 返回 501。
    rpent: 可选 ``RpentService``（RPent 兼容的 RPC 面）；注入后注册 **``POST /call``**
           （外部 agent 经它驱动 edge，见 wiki/design/motrix_edge_rpent_bridge.md），
           未注入时该端点回 ``ok=false`` 信封。
    """
    identity: Identity = load_identity(base_cfg)
    # 租约配置（``lease`` 段）：ttl = 租约有效期，renew_interval = 建议续租间隔
    lease_manager = lease_manager or build_lease_manager(base_cfg)
    # 上传会话（``upload`` 段）：本地 episode 扫描 / 选择 / 打包与上传队列状态
    uploads = uploads or UploadSession(base_cfg)

    services = Services(
        identity=identity,
        leases=lease_manager,
        uploads=uploads,
        node=node,
        commands=commands,
        meta=meta,
        preview=preview,
        webrtc=webrtc,
        rpent=rpent,
    )

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

    # ---- 统一错误处理 ----------------------------------------------------------
    #
    # 各层只抛自己的错误类型（``motrix_edge.errors.ServiceError`` 子类：命令层 ``CommandError``、
    # 租约层 ``LeaseError``、会话层 ``UploadError``、服务层各 ``*Error``）且只讲 **edge 错误码**；
    # **HTTP 状态码由本层维护**（:data:`_HTTP_STATUS`）—— 此处注册**一个**处理器，把 code 与
    # 人读原因一起渲染成 ``{"detail": ..., "code": ...}``。故路由层只需直接调 service。
    def _service_error_handler(request: Request, exc: Exception):
        code = getattr(exc, "code", ErrorCode.INTERNAL)
        return JSONResponse(
            status_code=_HTTP_STATUS.get(code, 500),
            content={"detail": str(exc), "code": getattr(code, "value", str(code))},
        )

    app.add_exception_handler(ServiceError, _service_error_handler)

    # ---- 挂载各域 router（HTTP 映射见 server/routes/*）--------------------------
    for router in build_routers(services):
        app.include_router(router)

    return app


__all__ = ["create_app"]
