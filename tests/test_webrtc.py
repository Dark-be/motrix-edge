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

"""server/webrtc WebRTC 推流测试 —— mock aiortc，无网络可跑。

覆盖：FrameStreamTrack 取帧（无帧发空白 / 缓存清空重发最近帧）、WebRTCService.offer
（租约校验 + answer，每相机一路视频轨道 + 推流频率检测）、POST /v1/webrtc/offer 端点
（501 / 409 / 200）。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from motrix_edge.frame import FrameManager
from motrix_edge.lease import Lease, LeaseManager, LeaseState
from motrix_edge.server import create_app
from motrix_edge.server.webrtc import FrameStreamTrack, WebRTCError, WebRTCService

BASE_CFG = {"identity": {"edge_id": "edge-test-001"}}


def install_test_lease(mgr: LeaseManager, lease_id: str = "ls_webrtc", ttl: float = 300) -> str:
    """部署 Console 签发的租约镜像（LeaseManager 直连），返回 lease_id。"""
    return mgr.install(
        Lease(
            lease_id=lease_id,
            edge_id="edge-test-001",
            holder_subject_id="operator-1",
            purpose="rollout",
            state=LeaseState.ACTIVE,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
            lease_version=1,
            ttl=ttl,
        )
    ).lease_id


class _FakeAnswer:
    sdp = "answer-sdp"
    type = "answer"


class _FakeSender:
    """mock RTCRtpSender：记录绑定 track，暴露可写的 _stream_id（aiortc 私有字段）。"""

    def __init__(self, track):
        self.track = track
        self._stream_id = "shared-stream"  # 模拟 aiortc 默认同一 PC 共享流 id


class _FakeTransceiver:
    def __init__(self, sender, kind="video"):
        self.sender = sender
        self.kind = kind


class _FakePC:
    """mock RTCPeerConnection：记录 addTrack / transceiver 列表、remote description，返回固定 answer。"""

    def __init__(self):
        self.tracks = []
        self.transceivers = []
        self.remote = None
        self.localDescription = _FakeAnswer()  # setLocalDescription 后取含候选的 SDP

    def addTrack(self, track):
        self.tracks.append(track)
        sender = _FakeSender(track)
        self.transceivers.append(_FakeTransceiver(sender))
        return sender

    def getTransceivers(self):
        return self.transceivers

    async def close(self):
        pass

    async def setRemoteDescription(self, desc):
        self.remote = desc

    async def createAnswer(self):
        return _FakeAnswer()

    async def setLocalDescription(self, answer):
        pass


@pytest.fixture
def mock_aiortc(monkeypatch):
    """mock aiortc 的 RTCPeerConnection / RTCSessionDescription（无网络、本机不协商）。"""
    import motrix_edge.server.webrtc as webrtc_mod

    monkeypatch.setattr(webrtc_mod, "RTCPeerConnection", _FakePC)
    monkeypatch.setattr(webrtc_mod, "RTCSessionDescription", lambda sdp, type: None)
    return webrtc_mod


def make_node(fm=None):
    """带 FrameManager 的 fake node（WebRTCService 经 node.frame_manager 取帧）。"""
    return SimpleNamespace(frame_manager=fm or FrameManager())


def test_frame_stream_track_sends_blank_without_frame(mock_aiortc):
    """无观测帧时 FrameStreamTrack 发送空白帧（尺寸 = FrameManager.image_size）。"""
    track = FrameStreamTrack(FrameManager())
    frame = asyncio.run(track.recv())
    assert frame.width == 320 and frame.height == 240


def test_frame_stream_track_reuses_last_frame_when_cache_empty(mock_aiortc):
    """无新帧时重发最近一帧（避免黑帧闪烁）：缓存清空后仍返回上次帧，而非空白黑帧。"""
    import cv2

    ok, buf = cv2.imencode(".jpg", np.full((8, 8, 3), 128, dtype=np.uint8))
    fm = FrameManager()
    fm.update({"observations/images/cam_head": buf.tobytes()})
    track = FrameStreamTrack(fm, "cam_head")
    first = track._latest_rgb()
    assert first.any()  # 有帧：非黑
    fm.clear()  # 清空缓存（如会话退出 / 空窗期）
    second = track._latest_rgb()
    assert second.any()  # 重发最近帧，仍非黑
    assert np.array_equal(first, second)


def test_offer_requires_lease(mock_aiortc):
    """WebRTCService.offer：未持有租约 → WebRTCError(409)。"""
    leases = LeaseManager()
    svc = WebRTCService(make_node(), leases=leases)
    with pytest.raises(WebRTCError) as ei:
        svc.offer(None, "offer-sdp")
    assert ei.value.status_code == 409


def test_offer_returns_answer(mock_aiortc):
    """持租约 + FrameManager 就绪 → offer 返回 answer SDP。"""
    leases = LeaseManager()
    svc = WebRTCService(make_node(), leases=leases)
    lease_id = install_test_lease(leases)
    result = svc.offer(lease_id, "offer-sdp")
    assert result == {"sdp": "answer-sdp", "type": "answer"}


def test_negotiate_adds_track_per_camera(mock_aiortc):
    """每相机一路视频轨道：FrameManager 有 2 路相机 → addTrack 2 次且绑定对应相机名。"""
    fm = FrameManager()
    fm.update(
        {
            "observations/qpos": np.zeros(2),
            "observations/images/cam_head": b"jpeg-bytes",
            "observations/images/cam_wrist": b"jpeg-bytes",
        }
    )
    leases = LeaseManager()
    svc = WebRTCService(make_node(fm), leases=leases)
    lease_id = install_test_lease(leases)
    svc.offer(lease_id, "offer-sdp")
    pc = svc._pc
    assert [t._camera_name for t in pc.tracks] == ["cam_head", "cam_wrist"]


def test_negotiate_gives_each_camera_distinct_stream_id(mock_aiortc):
    """每路相机独立 msid：answer 各 m= 段流 id 唯一（修复多相机显示同一流）。

    aiortc 默认把同一 PC 的所有 track 归入同一 MediaStream（同一 PC 级 __stream_id），
    修复在 setRemoteDescription 后为每路 video sender 覆盖独立 _stream_id → 浏览器分别建流。
    """
    fm = FrameManager()
    fm.update(
        {
            "observations/qpos": np.zeros(2),
            "observations/images/cam_head": b"jpeg-bytes",
            "observations/images/cam_wrist": b"jpeg-bytes",
            "observations/images/cam_gripper": b"jpeg-bytes",
        }
    )
    leases = LeaseManager()
    svc = WebRTCService(make_node(fm), leases=leases)
    lease_id = install_test_lease(leases)
    svc.offer(lease_id, "offer-sdp")
    pc = svc._pc
    stream_ids = [tr.sender._stream_id for tr in pc.getTransceivers() if tr.kind == "video"]
    assert len(stream_ids) == 3  # 每路相机一个 sender
    assert len(set(stream_ids)) == 3  # 流 id 全部唯一（不再共享同一 msid）


def test_negotiate_single_camera_fallback_gets_stream_id(mock_aiortc):
    """无相机观测兜底一路：仍为 sender 覆盖独立流 id（不崩）。"""
    leases = LeaseManager()
    svc = WebRTCService(make_node(), leases=leases)
    lease_id = install_test_lease(leases)
    svc.offer(lease_id, "offer-sdp")
    pc = svc._pc
    stream_ids = [tr.sender._stream_id for tr in pc.getTransceivers() if tr.kind == "video"]
    assert len(stream_ids) == 1
    assert stream_ids[0]  # 非空（独立流 id 已覆盖）


def test_webrtc_offer_endpoint_501_when_not_enabled():
    """未注入 WebRTCService：/v1/webrtc/offer → 501。"""
    client = TestClient(create_app(BASE_CFG))
    assert client.post("/v1/webrtc/offer", json={"sdp": "x"}).status_code == 501


def test_webrtc_offer_endpoint_requires_lease(mock_aiortc):
    """注入 WebRTCService 但未持有租约：/v1/webrtc/offer → 409。"""
    leases = LeaseManager()
    svc = WebRTCService(make_node(), leases=leases)
    client = TestClient(create_app(BASE_CFG, lease_manager=leases, webrtc=svc))
    assert client.post("/v1/webrtc/offer", json={"sdp": "x"}).status_code == 409


def test_webrtc_offer_endpoint_returns_answer(mock_aiortc):
    """持租约 → /v1/webrtc/offer 返回 Edge answer。"""
    leases = LeaseManager()
    svc = WebRTCService(make_node(), leases=leases)
    client = TestClient(create_app(BASE_CFG, lease_manager=leases, webrtc=svc))
    lease = install_test_lease(leases)
    r = client.post("/v1/webrtc/offer", json={"sdp": "offer-sdp"}, headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    body = r.json()
    assert body["sdp"] == "answer-sdp"
    assert body["type"] == "answer"
