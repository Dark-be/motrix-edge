# FrameManager 与 WebRTC 推流

## 摘要

`frame/` 的 **`FrameManager`** 统一管理 Edge 侧「最新观测帧缓存」（观测图像 + qpos，线程安全），
作为预览（`GET /v1/preview`）与 **WebRTC 推流**（`server/webrtc.py`）的单一帧来源。WebRTC 用
aiortc 标准信令（`POST /v1/webrtc/offer` 交换 SDP）：Edge 作为 Peer，**每路相机一个**
`FrameStreamTrack` 从 `FrameManager` 取对应相机最新帧（内部解码 jpeg → RGB）编码推流，浏览器
`<video>` 直接播放（无需网页解码 jpeg）。

## 目标

-   `FrameManager` 单点缓存最新帧（线程安全）：**node 持续观测写入**，preview / WebRTC 读取。
-   WebRTC 标准信令（offer / answer）推流；浏览器 `<video>` 直接播放。
-   观测语义（observe 只读缓存、不采集）见 [机器人适配器（adapter）](./motrix_edge_adapter.md)。

## FrameManager 契约

-   `update(obs)`：写入最新观测（`adapter.observe()` 返回）；摄像头帧**降采样为 Edge 侧尺寸
    `DEFAULT_IMAGE_SIZE = (320, 240)` JPEG**（`cache_observation`，质量 80）后缓存，方便内网传输。
-   `latest()`：读取最新观测帧（供 preview / WebRTC）；无帧返回 `None`。
-   `clear()`：清空缓存（会话结束 / 退出时）。
-   线程安全（`Lock`），单活跃最新帧。`image_size` 属性供 WebRTC 兜底空白帧用。

## 预览（GET /v1/preview）

| 端点              | 说明                                                                                                         |
| ----------------- | ------------------------------------------------------------------------------------------------------------ |
| `GET /v1/preview` | 最新观测：状态（有会话取会话状态，否则节点状态）/ adapter 身份 / observation（qpos + action + 摄像头名列表） |

响应：

```json
{
    "state": "ready",
    "adapter": { "name": "Test Robot", "type": "test_robot" },
    "observation": {
        "qpos": [0.1, 0.2],
        "action": [0.1, 0.2],
        "images": ["cam_head", "cam_left_wrist"]
    }
}
```

-   **图像不内联**：HTTP JSON 不承载二进制，图像（jpeg）由 WebRTC 推流到前端，preview 只返回
    摄像头名列表；qpos / action 为 `ndarray → list`。
-   受控操作：须持有有效租约（`X-Lease-Id`）；观测由节点级持续写入 **frame_manager（无需进入会话）**；无观测 → observation 为空。

## WebRTC 推流

`POST /v1/webrtc/offer`（body：网页 SDP offer；头 `X-Lease-Id`）：

```json
{ "sdp": "...", "type": "offer" }
```

响应（Edge answer）：`{ "sdp": "...", "type": "answer" }`。

实现要点：

-   **每路相机一个 track**：`FrameStreamTrack(frame_manager, camera_name)` 从 `FrameManager.latest()`
    取指定相机帧（观测图像 jpeg → 解码 RGB → `av.VideoFrame`）；无相机观测时兜底推一路空白帧。
-   **每路相机独立 msid**：aiortc 把同一 PC 的所有 track 归入**同一个 MediaStream**（PC 级
    `__stream_id`），answer 各 m= 段 msid 流 id 相同 → 浏览器把多路视频轨并入同一 stream，前端
    所有 `<video>` 显示同一路。修复：`setRemoteDescription` 后、`createAnswer` 前，为每路 video
    sender 覆盖**独立** `_stream_id`（aiortc 生成 answer 时读取），使每路 track 有独立 msid →
    浏览器分别为每路相机建 MediaStream（逐相机分离显示）。
-   **无新帧重发最近一帧**（缓存 `_last_rgb`，避免黑帧闪烁）；按 30fps 节流对齐 PTS。
-   **必须交换含候选的 SDP**：aiortc 的 ICE 候选在 `setLocalDescription` 内部 gather 后才写入
    `localDescription.sdp`；返回 `pc.localDescription.sdp`（而非 createAnswer 的原始 SDP），否则
    连接卡 `checking`。
-   **常驻事件循环**：aiortc 的 PC 后台任务（ICE / DTLS / 编码推流）需持续运行；`WebRTCService`
    维护后台事件循环线程（daemon），协商经 `run_coroutine_threadsafe` 提交，**不能用 `asyncio.run`**
    （会销毁循环、取消 PC 任务）。
-   单活跃 PC：每次 offer 关闭旧连接；受控操作须持有有效租约。

## 依赖

-   `aiortc`（+ `av` / PyAV）：WebRTC 信令 + 媒体编码。
-   `opencv-python`：jpeg 解码 / 缩放。

## 浏览器端查看

1. 启动 Edge：`uv run motrix-edge run`；进入任意会话（`session run capture` / `session run infer`）
   使 FrameManager 持续写入观测帧。
2. 前端 `edge-console`（或自包含 `frontend/webrtc_viewer.html`）创建 `RTCPeerConnection`
   （recvonly 视频）→ `createOffer` → `POST /v1/webrtc/offer`（头 `X-Lease-Id`）→ 拿 answer
   完成协商 → `<video>` 播放。

## 后续

-   多路相机切换 / 摄像头选择（当前每路相机一个 track，推流哪一路由前端选择）。
-   ICE candidate 交换（当前简化，单 offer/answer）；音频 / 双向数据通道（遥操作）。

## 相关文档

-   观测语义：[机器人适配器（adapter）](./motrix_edge_adapter.md)
-   会话写入帧缓存：[会话（session）](./motrix_edge_session.md)
-   WebRTC 端点注册：[HTTP 控制面（server）](./motrix_edge_server.md)
-   代码入口：`src/motrix_edge/frame/`（**feat/6**）、`src/motrix_edge/server/webrtc.py`（**feat/3**）—— 随对应分支落地
