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

"""统一坐标系外参：**采集（碰硬件）** + **解算（纯离线）** + **安装** + **自测**。

设计见 ``wiki/design/robot_pipeline_frames.md``；本脚本是那个流程的执行入口，三种用法::

    # ① 离线自测（不碰硬件、不要 pytest）：虚拟数据走完整求解链，必须复现真值
    python scripts/calibrate_extrinsics.py --self-test

    # ② 采集（**会连硬件 + 会动机械臂**）：先量准板、固定在工作台，再按提示逐帧采集
    python scripts/calibrate_extrinsics.py --collect --out /tmp/frames_samples.json

    # ③ 解算（纯离线，任意机器可跑；--install 直接写机器人进程读的那份产物）
    python scripts/calibrate_extrinsics.py --solve --samples /tmp/frames_samples.json
    python scripts/calibrate_extrinsics.py --solve --samples /tmp/frames_samples.json --install

⚠️ 采集注意事项（判读依据都在这里）：

- **板固定不动**（含探针触碰阶段）：探针得到的是「这块板」的位姿，板一挪全错；固定相机的多帧
  只用于**平均降噪**（帧间离散偏大会告警），腕相机的多姿态靠**臂动**而不是板动；
- **姿态要铺开**：腕相机要摆在 ≥ 10 个位姿，**朝向差异 ≥ 20–30°**（全朝一个方向小幅度摆时手眼
  标定退化——残差看着小，解不准）。判据在解算报告里的 ``rotation_spread_deg``；
- **触碰点要铺开**：探针在板的不同区域 / 不同高度各碰 3–6 点（近似共线 → ``points_condition``
  接近 0，解不可靠）；
- **静止采样**：三台相机各自取帧、**没有跨相机硬件同步**，运动中的「同一时刻」不存在；
- **开重力前馈**（若已标定）：减小 MIT 稳态误差，提升探针触碰精度；
- **退出会话 / 关闭遥操作**：采集要独占硬件（`robot.connect()` 直连，机器人进程必须先停）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config import load_config  # noqa: E402
from robot import get_robot  # noqa: E402
from robot.calibration import (  # noqa: E402
    FRAMES_RELATIVE,
    BoardSpec,
    CameraView,
    ProbeTouch,
    Samples,
    detect,
    save_frames,
    self_test,
    solve_frames,
)
from robot.calibration.samples import as_pose_list  # noqa: E402

from motrix_edge.geometry import (  # noqa: E402
    WORLD_ALIAS,
)

#: 每台相机建议的最少帧数（少于它解算仍会跑，但报告会提醒补采）。
MIN_VIEWS = 6
#: 每个臂建议的最少触碰点数（解算要求 ≥ 3）。
MIN_TOUCHES = 3


def _cv2():
    import cv2  # noqa: PLC0415 只有采集路径需要

    return cv2


def _to_image(color):
    """观测里的彩色帧 → BGR 图像（机器人按配置可能给 JPEG bytes，也可能给原始 ndarray）。"""
    if isinstance(color, (bytes, bytearray, memoryview)):
        cv2 = _cv2()
        return cv2.imdecode(np.frombuffer(bytes(color), dtype=np.uint8), cv2.IMREAD_COLOR)
    array = np.asarray(color)
    if array.ndim == 3 and array.shape[2] == 3:
        return array
    raise RuntimeError(f"unexpected color frame shape {array.shape}")


def _intrinsics_of(robot, camera: str) -> dict:
    """取该相机的彩色内参（``camera_meta()``：与 ``GET /v1/cameras`` 同一来源）。"""
    for info in robot.camera_meta():
        if str(info.get("name")) == camera:
            block = dict(info.get("intrinsics") or {})
            missing = [key for key in ("fx", "fy", "cx", "cy") if not block.get(key)]
            if missing:
                raise RuntimeError(f"{camera}: 内参缺失 {missing}（相机是否已连线 / 标定？）")
            return {key: float(block[key]) for key in ("fx", "fy", "cx", "cy")}
    raise RuntimeError(f"{camera}: camera_meta() 里没有这台相机")


def _poses_by_arm(robot, flat_pose) -> dict[str, list[float]]:
    """位姿向量 → 按臂名的 6 维列表（顺序与 ``ARM_NAMES`` 对齐，每臂 ``POSE_DIM_PER_ARM`` 维）。"""
    values = np.asarray(flat_pose, dtype=np.float64).reshape(-1)
    per_arm = int(getattr(robot, "POSE_DIM_PER_ARM", 6))
    arms = [str(arm) for arm in getattr(robot, "ARM_NAMES", ())]
    if per_arm <= 0 or not arms or values.size < per_arm * len(arms):
        return {}
    blocks = values[: per_arm * len(arms)].reshape(len(arms), per_arm)
    return {arm: [float(value) for value in blocks[index, :6]] for index, arm in enumerate(arms)}


# ---- 采集 --------------------------------------------------------------------


def _collect_camera_views(robot, samples: Samples, camera: str, spec: BoardSpec) -> None:
    """交互采集一台相机的「看板」帧：回车记录一帧（检不到角点会提示重摆）。"""
    index = list(robot.IMAGE_NAMES).index(camera)
    arm = samples.wrist_cameras.get(camera)
    print(f"\n=== {camera}（{'腕相机 · 臂 ' + arm if arm else '固定相机'}）===")
    if arm:
        print("  把该臂依次摆到 10 个以上位姿（板始终在视野内，朝向尽量分散）")
    else:
        print("  板**保持不动**，静止多拍几帧（多帧只用于平均降噪）")
        print("  ⚠️ 不要挪板：探针那一步记的是「这块板」的位姿，挪了就对不上")
    while True:
        answer = input(f"  [{camera}] 回车记录一帧，q 结束：").strip().lower()
        if answer in {"q", "quit", "exit"}:
            return
        frames = robot.get_observation_frames()
        color = frames[index].get("color")
        image = _to_image(color)
        corners, ids = detect(image, spec)
        if len(ids) < 4:
            print(f"  ✗ 只检出 {len(ids)} 个角点（板要完整入画、别太斜、别糊）——本帧丢弃")
            continue
        pose = _poses_by_arm(robot, robot.get_observation_pose())
        samples.views.append(
            CameraView(
                camera=camera,
                corners=corners,
                ids=ids,
                arm=arm,
                pose=None if arm is None else as_pose_list(pose.get(arm)),
            )
        )
        print(f"  ✓ 第 {len(samples.views_of(camera))} 帧：{len(ids)} 个角点")


def _collect_touches(robot, samples: Samples, spec: BoardSpec, arm: str) -> None:
    """交互采集该臂的探针触碰：把探针尖点到板上某个角点，记录「哪个角点 + 当前位姿」。"""
    from robot.calibration.board import object_points

    points = object_points(spec)
    print(f"\n=== {arm} 臂 · 探针触碰 ===")
    print("  板上角点 id → 板系坐标（米）——挑不同区域 / 不同高度的点各碰一下：")
    for corner_id, point in enumerate(points):
        print(f"    id={corner_id:2d}  ({point[0]:+.3f}, {point[1]:+.3f}, {point[2]:+.3f})")
    while True:
        answer = input(f"  [{arm}] 探针尖对准某个角点后回车记录，q 结束：").strip().lower()
        if answer in {"q", "quit", "exit"}:
            return
        raw = input(f"  [{arm}] 碰到的是哪个角点 id？").strip()
        try:
            corner_id = int(raw)
            point = points[corner_id]
        except (ValueError, IndexError):
            print(f"  ✗ 角点 id 非法：{raw!r}（应在 0..{len(points) - 1}）")
            continue
        pose = _poses_by_arm(robot, robot.get_observation_pose())
        samples.touches.append(
            ProbeTouch(
                arm=arm, pose=as_pose_list(pose.get(arm)), point=[float(v) for v in point], label=f"corner_{corner_id}"
            )
        )
        print(f"  ✓ 第 {len(samples.touches_of(arm))} 次触碰：corner_{corner_id}")


def collect(args) -> int:
    """连硬件采集：相机看板 + 探针触碰 → 样本 JSON。"""
    cfg = load_config(args.config, args.machine)
    spec = BoardSpec(square_length=args.square_length, marker_length=args.marker_length, dictionary=args.dictionary)
    robot = get_robot(cfg)
    samples = Samples(board=spec, world_arm=args.world_arm, robot=str(getattr(robot, "name", "")))
    samples.arms = [str(arm) for arm in getattr(robot, "ARM_NAMES", ())]
    samples.wrist_cameras = {str(name): str(arm) for name, arm in dict(getattr(robot, "WRIST_CAMERAS", {})).items()}

    print("⚠️ 采集会连接硬件。请确认：机器人进程已停、会话已退出、遥操作已关、急停可达。")
    print(f"   机型={cfg.get('robot', {}).get('type')} 臂={samples.arms} 腕相机={samples.wrist_cameras}")
    if input("   继续？(yes/no) ").strip().lower() not in {"y", "yes"}:
        print("已取消。")
        return 1

    robot.connect()
    try:
        for camera in robot.IMAGE_NAMES:
            samples.intrinsics[camera] = _intrinsics_of(robot, camera)
        for camera in robot.IMAGE_NAMES:
            _collect_camera_views(robot, samples, camera, spec)
        for arm in samples.arms:
            _collect_touches(robot, samples, spec, arm)
    finally:
        try:
            robot.safe_stop()
        finally:
            robot.disconnect()

    path = samples.save(args.out)
    print("\n=== 采集完成 ===")
    for line in samples.summary():
        print(f"  {line}")
    print(f"  样本已写入：{path}")
    print(f"  下一步：python scripts/calibrate_extrinsics.py --solve --samples {path}")
    return 0


# ---- 解算 --------------------------------------------------------------------


def solve(args) -> int:
    """纯离线解算：样本 JSON → ``frames.json``（可选 ``--install``）。

    解算主体在库里（``robot.calibration.pipeline.solve_frames``）——脚本只负责 I/O 与报告，
    求解链能离线单测（合成样本 → 复现真值）。
    """
    samples = Samples.load(args.samples)
    frames = solve_frames(samples, log=print)
    print("\n=== 结果 ===")
    print(f"  world = {frames.world}（别名 {WORLD_ALIAS}）")
    for arm in sorted(frames.arms):
        tip = frames.probe_tip(arm)
        tip_text = "—" if tip is None else f"({tip[0]:+.3f}, {tip[1]:+.3f}, {tip[2]:+.3f})"
        print(f"  arm {arm}: T_world_base 已解出 · 探针尖（法兰系）{tip_text}")
    for name in sorted(frames.cameras):
        item = frames.cameras[name]
        rms = "—" if item.rms_m is None else f"{item.rms_m * 1000:.2f} mm"
        print(f"  cam {name}: {item.mount} · 源帧 {frames.camera_frame(name)} · RMS {rms}")

    if args.install:
        path = save_frames(frames)
        print(f"\n已安装到：{path}")
        print("⚠️ 重启机器人进程（外参随 GET /v1/cameras 上报、Edge 侧会缓存）后生效。")
        print("   验收：python scripts/verify_extrinsics.py --host <edge> --port <port> --lease <lease_id>")
    else:
        out = Path(args.out or "frames.json")
        frames.save(out)
        print(f"\n已写出：{out}（加 --install 可直接安装到配置目录）")
    return 0


# ---- 入口 --------------------------------------------------------------------


def _run_self_test() -> int:
    """虚拟数据自测（不碰硬件、不要 pytest）：约定写错时**在真机之前**就暴露。"""
    report = self_test()
    print("=== 虚拟数据自测（必须复现真值）===")
    for key, value in report.items():
        print(f"  {key}: {value}")
    failures = [
        name
        for name, value in (
            ("hand_eye_error", report["hand_eye_error"]),
            ("board_error", max(report["board_error"].values())),
            ("probe_tip_error", max(report["probe_tip_error"].values())),
        )
        if float(value) > 1e-9
    ]
    if failures:
        print(f"✗ 自测失败：{failures}（求解链有约定错，别上真机）")
        return 1
    print("✓ 自测通过（手眼 / 板外参 / 探针尖 都复现到 1e-9 以内）")
    return 0


# ---- 入口 --------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="统一坐标系外参：采集 / 解算 / 安装 / 自测（见 wiki/design/robot_pipeline_frames.md）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--collect", action="store_true", help="连硬件采集（会动机械臂）→ 样本 JSON")
    mode.add_argument("--solve", action="store_true", help="纯离线解算样本 JSON → frames.json")
    mode.add_argument("--self-test", action="store_true", help="虚拟数据自测（不碰硬件、不要 pytest）")
    parser.add_argument("--samples", default=None, help="--solve 的输入样本 JSON")
    parser.add_argument("--out", default=None, help="--collect 的样本输出（默认 frames_samples.json）")
    parser.add_argument("--install", action="store_true", help=f"--solve 时直接写产物（{FRAMES_RELATIVE}）")
    parser.add_argument("--config", default="test_robot", help="--collect 的机型配置名（默认 test_robot）")
    parser.add_argument("--machine", default=None, help="--collect 的机器档案（只写差异的那份）")
    parser.add_argument("--world-arm", default="left", help="world 基底取哪条臂的基座（默认 left）")
    parser.add_argument("--square-length", type=float, default=0.04, help="板格子边长（米，默认 0.04）")
    parser.add_argument("--marker-length", type=float, default=0.02, help="板内 ArUco 码边长（米，默认 0.02）")
    parser.add_argument("--dictionary", default="DICT_5X5_50", help="ArUco 字典名（默认 DICT_5X5_50）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.self_test:
        return _run_self_test()
    if args.collect:
        args.out = args.out or "frames_samples.json"
        return collect(args)
    if not args.samples:
        _parser().error("--solve 需要 --samples <样本 JSON>（先跑 --collect）")  # 打印用法并以 2 退出
    return solve(args)


if __name__ == "__main__":
    raise SystemExit(main())
