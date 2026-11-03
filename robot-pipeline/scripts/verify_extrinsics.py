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

"""统一坐标系外参**验收**：走 Edge 的 HTTP 面（``GET /v1/depth``），验的就是运行期那一条通路。

设计见 ``wiki/design/robot_pipeline_frames.md``「标定流程 · 验收」::

    # 跨相机一致性（主指标）：同一个**物理点**在各相机预览里点到的归一化坐标各输一次
    python scripts/verify_extrinsics.py --host 127.0.0.1 --port 8000 --lease <lease_id>

    # 另加绝对精度：让左臂把探针尖停在同一测点上（需产物含 probe_tip，且机器人进程在跑）
    python scripts/verify_extrinsics.py --host 127.0.0.1 --port 8000 --lease <lease_id> --probe-arm left

判读：

- **跨相机一致性**（主指标）：同一物理点被不同相机各自反投影到 ``world`` 后，两两距离的 RMS / 最大
  值——目标 **RMS < 5 mm** @0.5–1 m（RealSense 深度噪声本身就在 mm 量级）；
- **绝对精度**：与「探针尖的世界位置」对比（``T_world_base · FK(q) · t_probe``，偏移来自产物）——
  期望 5–15 mm（含 MIT 稳态误差与探针尖对准误差）；
- ⚠️ **只在板附近测 = 自证**：在工作区换 2–3 个区域各测一遍；
- ⚠️ 三台相机**没有跨相机硬件同步**，请**静止**测量（运动中不同拍的同一物理点本来就不重合）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from motrix_edge.geometry import pose_to_transform, transform_points  # noqa: E402


def _get(url: str, *, lease: str | None = None, params: dict | None = None) -> dict:
    """本地 HTTP GET（只用标准库——现场脚本不值得再加依赖）；HTTP 错误直接终止并打印回执。"""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {} if lease is None else {"X-Lease-Id": lease}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5.0) as response:  # noqa: S310
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{url} → HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc


def _depth(host: str, port: int, lease: str, camera: str, u: float, v: float) -> dict:
    return _get(f"http://{host}:{port}/v1/depth", lease=lease, params={"camera": camera, "u": u, "v": v})


def _preview(host: str, port: int, lease: str) -> dict:
    return _get(f"http://{host}:{port}/v1/preview", lease=lease)


def _available_cameras(host: str, port: int, lease: str) -> list[str]:
    """有深度的相机名（``/v1/preview`` 的 ``observation.depth``）——坐标查询只对它们有意义。"""
    observation = _preview(host, port, lease).get("observation") or {}
    return [str(name) for name in (observation.get("depth") or [])]


def _pairwise_distances(points: dict[str, list[float]]) -> tuple[float, float]:
    """同一物理点在各相机下的 ``world`` 坐标 → ``(RMS, 最大值)``（两两距离）。"""
    names = sorted(points)
    values = [np.asarray(points[name], dtype=np.float64) for name in names]
    distances = [
        float(np.linalg.norm(values[i] - values[j])) for i in range(len(names)) for j in range(i + 1, len(names))
    ]
    if not distances:
        return 0.0, 0.0
    array = np.asarray(distances, dtype=np.float64)
    return float(np.sqrt(np.mean(array**2))), float(np.max(array))


def _query_point(host: str, port: int, lease: str, cameras: list[str]) -> dict[str, list[float]]:
    """交互输入「同一物理点」在各相机预览里的**归一化**坐标 → 各自的 ``world`` 坐标。"""
    print("\n在预览（控制台 / WebRTC）里找到**同一个物理点**，逐台相机输入它的归一化坐标 [0,1]：")
    points: dict[str, list[float]] = {}
    for camera in cameras:
        raw = input(f"  {camera} 的 u,v（留空跳过该相机；q 结束）：").strip()
        if raw.lower() in {"q", "quit", "exit"}:
            return {}
        if not raw:
            continue
        try:
            u, v = (float(value) for value in raw.replace(",", " ").split())
        except ValueError:
            print(f"  ✗ 解析失败：{raw!r}（期望形如 0.42 0.55）")
            continue
        body = _depth(host, port, lease, camera, u, v)
        if body.get("xyz_world") is None:
            reason = "该像素无有效深度" if not body.get("valid") else "无外参（未标定 / 该相机未标定）"
            print(f"  ✗ {camera}: {reason}（depth_m={body.get('depth_m')}）")
            continue
        world = [float(value) for value in body["xyz_world"]]
        points[camera] = world
        print(f"  ✓ {camera}: world = ({world[0]:+.4f}, {world[1]:+.4f}, {world[2]:+.4f}) m")
    return points


def _robot_frames(host: str, port: int) -> dict | None:
    """机器人进程 ``GET /v1/cameras`` 的 ``frames`` 段（标定产物原样；未标定 → ``None``）。

    绝对精度那一步需要 ``T_world_base`` 与探针尖偏移——Edge 的 ``/v1/depth`` 只给坐标，产物本身在
    机器人进程那边（``--robot-port``；进程没起 / 老版本无该段时该步自动跳过）。
    """
    try:
        return _get(f"http://{host}:{port}/v1/cameras").get("frames")
    except SystemExit as exc:
        print(f"  （读机器人外参失败：{exc}）")
        return None


def _pose_by_arm(observation: dict) -> dict[str, list[float]]:
    """``/v1/preview`` 的 ``observation.pose``（每臂 6 维扁平）→ 按臂名的列表。"""
    pose = observation.get("pose")
    arms = [str(arm) for arm in (observation.get("arms") or [])]
    if not pose or not arms:
        return {}
    values = np.asarray(pose, dtype=np.float64).reshape(len(arms), -1)
    return {arm: values[index, :6].tolist() for index, arm in enumerate(arms)}


def _probe_tip_world(frames_payload: dict, arm: str, pose_by_arm: dict[str, list[float]]) -> np.ndarray | None:
    """探针尖的 ``world`` 位置：``T_world_base · FK(q) · t_probe``（缺任一项 → ``None``）。"""
    block = dict((frames_payload.get("arms") or {}).get(arm) or {})
    tip = block.get("probe_tip")
    base = block.get("T_world_base")
    pose = pose_by_arm.get(arm)
    if tip is None or base is None or pose is None:
        print(f"  （{arm}: 产物缺 T_world_base / probe_tip，或该臂无当前位置——跳过绝对精度）")
        return None
    matrix = np.asarray(base, dtype=np.float64).reshape(4, 4) @ pose_to_transform(pose)
    return transform_points(matrix, np.asarray(tip, dtype=np.float64).reshape(3))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统一坐标系外参验收（走 Edge HTTP）")
    parser.add_argument("--host", default="127.0.0.1", help="Edge 地址")
    parser.add_argument("--port", type=int, default=8000, help="Edge 端口")
    parser.add_argument("--lease", required=True, help="Edge 租约 id（受控操作；控制台里签发）")
    parser.add_argument("--camera", action="append", default=None, help="只验这几台相机（可重复；缺省全部有深度的）")
    parser.add_argument("--rounds", type=int, default=3, help="测几个位置（建议换区域，默认 3）")
    parser.add_argument("--robot-host", default=None, help="机器人进程地址（默认同 --host；用于绝对精度）")
    parser.add_argument("--robot-port", type=int, default=8090, help="机器人进程端口（默认 8090）")
    parser.add_argument("--probe-arm", default=None, help="绝对精度：用这条臂的探针尖对比（需产物含 probe_tip）")
    args = parser.parse_args(argv)

    cameras = args.camera or _available_cameras(args.host, args.port, args.lease)
    if len(cameras) < 2:
        print(f"✗ 需要至少两台有深度的相机（当前：{cameras}）——先确认机器人开了深度、Edge 已连接")
        return 1
    print(f"验收相机：{cameras}")
    print("⚠️ 三台相机没有跨相机硬件同步：请**静止**测量；并换 2–3 个区域各测一遍（别只在板附近）。")

    rms_values: list[float] = []
    max_values: list[float] = []
    absolute_errors: list[float] = []
    frames_payload = _robot_frames(args.robot_host or args.host, args.robot_port) if args.probe_arm else None
    for round_index in range(1, args.rounds + 1):
        print(f"\n=== 第 {round_index}/{args.rounds} 个位置 ===")
        points = _query_point(args.host, args.port, args.lease, cameras)
        if not points:
            break
        rms, worst = _pairwise_distances(points)
        rms_values.append(rms)
        max_values.append(worst)
        print(f"  一致性：RMS {rms * 1000:.2f} mm · 最大 {worst * 1000:.2f} mm（{len(points)} 台相机）")
        if args.probe_arm and frames_payload:
            observation = _preview(args.host, args.port, args.lease).get("observation") or {}
            tip_world = _probe_tip_world(frames_payload, args.probe_arm, _pose_by_arm(observation))
            if tip_world is not None:
                reference = np.mean([np.asarray(value, dtype=np.float64) for value in points.values()], axis=0)
                error = float(np.linalg.norm(tip_world - reference))
                absolute_errors.append(error)
                print(f"  绝对精度：探针尖 ←→ 深度坐标 {error * 1000:.2f} mm")

    if not rms_values:
        print("没有采集到有效测点。")
        return 1
    overall_rms = float(np.sqrt(np.mean(np.square(rms_values))))
    print(f"\n=== 汇总（{len(rms_values)} 个位置）===")
    print(f"  跨相机一致性 RMS {overall_rms * 1000:.2f} mm · 最大 {np.max(max_values) * 1000:.2f} mm")
    print(f"  目标：RMS < 5 mm @0.5–1 m；{'✓ 通过' if overall_rms < 0.005 else '✗ 偏大'}")
    if absolute_errors:
        print(
            f"  绝对精度（与 {args.probe_arm} 臂探针尖对比）均值 {np.mean(absolute_errors) * 1000:.2f} mm"
            f" · 最大 {np.max(absolute_errors) * 1000:.2f} mm（期望 5–15 mm）"
        )
    if overall_rms >= 0.005:
        print("  偏大时的排查顺序：① 换区域复测（只在板附近测会自证）② 看逐相机 RMS 与帧间离散")
        print("  ③ 板是否固定、采样时是否静止 ④ 腕相机姿态铺开度是否 ≥ 20°")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
