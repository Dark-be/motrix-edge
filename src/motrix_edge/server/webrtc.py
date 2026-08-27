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

"""WebRTC 推流 —— aiortc **video track**（H264） + 信令处理。

Edge 作为 WebRTC Peer：`POST /v1/webrtc/offer` 接收网页 SDP offer，创建
``RTCPeerConnection``，**每路相机一个 ``FrameStreamTrack``**（从 FrameManager 取对应
相机最新观测帧，解码 jpeg → RGB → ``av.VideoFrame``），返回 answer。浏览器 ``<video>``
直接播放（aiortc 编码推流）。
"""

import asyncio
import fractions
import threading
import time
import uuid

import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame

from motrix_edge.adapter.base import CAMERA_PREFIX
from motrix_edge.lease import LeaseError, LeaseManager

# 协商超时（秒）：offer/answer 需在时限内完成
_NEGOTIATE_TIMEOUT = 15.0


class FrameStreamTrack(VideoStreamTrack):
    """从 FrameManager 取**指定相机**的观测帧推流：观测图像（jpeg）→ 解码 RGB → ``av.VideoFrame``。

    **无新帧时重发最近一帧**（缓存 ``_last_rgb``，避免推流节奏快于观测写入导致的黑帧
    闪烁）；仅从未收到任何帧时才发空白帧。``camera_name=None`` 时取第一路相机（兜底）。
    """

    def __init__(self, frame_manager, camera_name: str | None = None):
        super().__init__()
        self._manager = frame_manager
        self._camera_name = camera_name
        self._last_rgb: np.ndarray | None = None  # 最近一帧（无新帧时重发）
        self._fps = 30  # 推流帧率：recv 按此节奏对齐（与 PTS 步进一致，避免编码器失配）
        self._start = fractions.Fraction(1, self._fps)  # 覆写 aiortc 默认 30fps PTS 步进
        self._last_emit = 0.0
        self.kind = "video"

    async def recv(self):
        # 帧率节流：按 _fps（30Hz）对齐真实推流节奏。若 aiortc 高速拉帧而 recv 不节流，
        # 编码器实际帧速率会远超 PTS 步进 → rate control 失配 → 周期性坏帧 / 黑帧。
        now = time.monotonic()
        wait = self._last_emit + 1 / self._fps - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_emit = time.monotonic()

        pts, time_base = await self.next_timestamp()
        rgb = self._latest_rgb()
        av_frame = VideoFrame.from_ndarray(rgb, format="rgb24")
        av_frame.pts = pts
        av_frame.time_base = time_base
        return av_frame

    def _latest_rgb(self) -> np.ndarray:
        """取指定相机最新观测帧（jpeg → RGB）；无新帧重发最近一帧，从未收到才发空白。"""
        frame = self._manager.latest() or {}
        key = f"{CAMERA_PREFIX}{self._camera_name}" if self._camera_name else None
        for k, value in frame.items():
            if key is not None:
                if k != key:
                    continue
            elif not k.startswith(CAMERA_PREFIX):
                continue
            if isinstance(value, (bytes, bytearray)):
                bgr = cv2.imdecode(np.frombuffer(bytes(value), dtype=np.uint8), cv2.IMREAD_COLOR)
                if bgr is not None:
                    self._last_rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                    return self._last_rgb
        # 无新帧（缓存未写入 / 会话切换间隙 / 该相机缺帧）：重发最近一帧，避免黑帧闪烁
        if self._last_rgb is not None:
            return self._last_rgb
        width, height = self._manager.image_size
        return np.zeros((height, width, 3), dtype=np.uint8)


class WebRTCError(Exception):
    """WebRTC 操作被拒绝（租约缺失 / 不匹配 / 过期 / 未启用）。携带 HTTP status_code。"""

    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


class WebRTCService:
    """WebRTC 推流服务：绑定 FrameManager（经 node）+ 租约校验，处理 SDP offer/answer。

    单活跃 PeerConnection：每次 offer 创建新连接（旧的关闭）。**常驻后台事件循环**：
    aiortc 的 PC 依赖后台任务（ICE / DTLS / 编码推流）持续运行，不能随单个请求的
    ``asyncio.run`` 销毁事件循环；所有协商操作经 ``run_coroutine_threadsafe`` 提交到
    常驻循环，PC 存活其中，跨请求持续推流。
    """

    def __init__(self, node, leases: LeaseManager | None = None):
        self._node = node  # 正在运行的 EdgeNode（frame_manager 取自 node）
        self._leases = leases or LeaseManager()  # Edge 级租约（受控操作校验）
        self._pc: RTCPeerConnection | None = None  # 当前 PeerConnection（单活跃）
        # 常驻后台事件循环（daemon 线程）：PC 的后台任务存活于此，跨请求持续工作
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, name="webrtc-io", daemon=True)
        self._loop_thread.start()

    def _run_loop(self):
        """后台线程：绑定事件循环并持续运行（直到 close）。"""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def close(self):
        """停止后台事件循环（生命周期清理；daemon 线程随进程退出兜底）。"""
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def offer(self, lease_id: str | None, sdp: str, sdp_type: str = "offer") -> dict:
        """接收网页 SDP offer，创建 PeerConnection + 每相机一路视频轨道，返回 answer SDP。

        受控操作：须持有有效租约；FrameManager 未就绪 → 501。协商在常驻后台循环执行。
        """
        try:
            self._leases.require(lease_id)
        except LeaseError as exc:
            raise WebRTCError(str(exc), exc.status_code) from exc

        frame_manager = getattr(self._node, "frame_manager", None)
        if frame_manager is None:
            raise WebRTCError("frame manager not available", status_code=501)

        future = asyncio.run_coroutine_threadsafe(self._negotiate(frame_manager, sdp, sdp_type), self._loop)
        try:
            return future.result(timeout=_NEGOTIATE_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise WebRTCError("webrtc negotiation timeout", status_code=504) from exc

    async def _negotiate(self, frame_manager, sdp: str, sdp_type: str) -> dict:
        """异步协商：关闭旧连接 → 创建 PC + **每相机一路视频轨道** → offer → answer。

        相机集合来自 FrameManager 最新观测（``observations/images/<name>``）；无相机时
        兜底推一路（``FrameStreamTrack(camera_name=None)``，发空白帧）。
        """
        if self._pc is not None:
            await self._pc.close()
        pc = RTCPeerConnection()
        self._pc = pc

        latest = frame_manager.latest() or {}
        camera_names = [k[len(CAMERA_PREFIX) :] for k in latest if k.startswith(CAMERA_PREFIX)]
        if not camera_names:
            camera_names = [None]  # 无相机观测：兜底一路（空白帧）
        for name in camera_names:
            pc.addTrack(FrameStreamTrack(frame_manager, name))

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        # aiortc 把同一 PC 的所有 track 归入**同一个 MediaStream**（``_createTransceiver``
        # 统一赋 PC 级 ``__stream_id``），answer SDP 各 m= 段 msid 的流 id 相同 → 浏览器把
        # 多路视频轨并入同一 stream，前端 ``e.streams[0]`` 全是同一个流 → 所有画面显示同一路。
        # 修复：setRemoteDescription 后、createAnswer 前，给每路 video sender 覆盖**独立**
        # ``_stream_id``（aiortc 生成 answer 时读取它），使每路 track 有独立 msid → 浏览器
        # 分别为每路相机建 MediaStream（逐相机分离显示）。
        for i, transceiver in enumerate(pc.getTransceivers()):
            if transceiver.kind == "video" and transceiver.sender.track is not None:
                # 依赖 aiortc **私有字段** RTCRtpSender._stream_id（无公开 API 为单条 track 单独
                # 设 msid）。实现随 aiortc 内部结构变化可能失效 → pyproject 已对 aiortc 加版本上限
                # （aiortc>=1.15.0,<2），升级前须回归本多相机独立 msid 测试。
                transceiver.sender._stream_id = f"cam-{i}-{uuid.uuid4()}"
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        # 候选在 setLocalDescription 内部 gather 后才写入 localDescription.sdp：
        # 必须返回 localDescription（含 ICE candidate），而非 createAnswer 的原始 SDP
        local = pc.localDescription
        return {"sdp": local.sdp, "type": local.type}
