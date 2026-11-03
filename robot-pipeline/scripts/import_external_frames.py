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

"""把**外部工具**采的标定数据导成我们的 ``frames.json``（纯离线，不碰硬件）。

适用场景：现场采集不是用我们的 ``calibrate_extrinsics.py`` 做的（例如
``piper_camera_calibration`` 那套），样本字段不同但信息等价（角点 + 位姿 + 板参 + 内参），而且
**只覆盖了一条臂**。本脚本把两条腿都**用我们自己的求解链重算**（不读对方的 ``handeye.yaml``），
再按装配对称假设把 ``world``（左臂基座）锚上，最后写出产物。

用法（现场那批数据的目录天然适配，``--root`` 就够）::

    # ① 先看一遍判读（不写任何东西）
    python scripts/import_external_frames.py --root /path/to/piper_camera_calibration

    # ② 写出产物（--install 直接写机器人进程读的那一份）
    python scripts/import_external_frames.py --root /path/to/piper_camera_calibration --out /tmp/frames.json
    python scripts/import_external_frames.py --root /path/to/piper_camera_calibration --install

期望的目录布局（``--wrist-session`` / ``--fixed-session`` / ``--factory`` 可逐个覆盖）::

    <root>/factory/<wrist-intrinsics>.yaml      出厂内参（整流图，零畸变）
    <root>/factory/<fixed-intrinsics>.yaml
    <root>/handeye_*/<相机>/<时间戳>/manifest.json + samples/sample_*.json    手眼（板静止、臂摆位姿）
    <root>/head_via_wrist/*/<时间戳>/manifest.json + samples/sample_*.json    固定板 + 双相机同拍

⚠️ **这条链的 ``world`` 锚定是装配假设**（左右臂相对 head 相机镜像 + 两底座坐标系同向），
不是标定结果：默认 ``--plane vertical`` 按「同底板 ⇒ 基座同高」把相机安装倾斜的 z 残差消掉，
但对称面是否过相机光心、两底座是否真的同向，仍要跟实物/图纸核对。要 mm 级请做左臂探针实测
（见 ``wiki/design/robot_pipeline_frames.md``）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from robot.calibration import (  # noqa: E402
    FRAMES_RELATIVE,
    KIND_FIXED_BOARD_BRIDGE,
    KIND_HAND_EYE,
    ExternalSession,
    import_frames,
    inherit_wrist_camera,
    load_intrinsics,
    save_frames,
)
from robot.calibration.external import (  # noqa: E402
    DEFAULT_ARM,
    DEFAULT_FIXED_NAME,
    DEFAULT_WORLD_ARM,
    DEFAULT_WRIST_NAME,
)

#: 自动探测会话用的 glob（``<root>/<它>/manifest.json`` 的 ``session_type`` 决定收哪个）。
SESSION_GLOBS = {"handeye": "handeye_*/*/*", "bridge": "head_via_wrist/*/*"}


def _session_from(root: Path, pattern: str, kind: str) -> Path | None:
    """在 ``root`` 下按 glob 找 ``session_type == kind`` 的会话目录（找不到 → ``None``）。"""
    for path in sorted(root.glob(pattern)):
        manifest = path / "manifest.json"
        if not manifest.exists():
            continue
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 坏的 manifest 不参与探测（后面读取会给出明确报错）
            continue
        if str(payload.get("session_type")) == kind:
            return path
    return None


def _resolve(args) -> tuple[Path, Path | None, Path]:
    """解析三个目录：手眼会话（必需）/ 固定板会话（锚定必需）/ 内参目录。"""
    root = Path(args.root) if args.root else None
    wrist = Path(args.wrist_session) if args.wrist_session else None
    fixed = Path(args.fixed_session) if args.fixed_session else None
    factory = Path(args.factory) if args.factory else None

    if root is not None:
        wrist = wrist or _session_from(root, SESSION_GLOBS["handeye"], KIND_HAND_EYE)
        fixed = fixed or _session_from(root, SESSION_GLOBS["bridge"], KIND_FIXED_BOARD_BRIDGE)
        factory = factory or (root / "factory")
    if wrist is None:
        raise SystemExit("找不到手眼会话：给 --root（含 handeye_*/）或显式 --wrist-session")
    if factory is None:
        raise SystemExit("找不到内参目录：给 --root（含 factory/）或显式 --factory")
    if fixed is None:
        raise SystemExit("找不到固定板 + 双相机会话（--fixed-session）：没有它无法把 world 锚上")
    return wrist, fixed, factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="外部采集数据 → 我们的 frames.json（纯离线；见 wiki/design/robot_pipeline_frames.md）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", default=None, help="外部数据根（自动探测 factory/ 与两个会话）")
    parser.add_argument("--wrist-session", default=None, help="手眼会话目录（含 manifest.json）")
    parser.add_argument("--fixed-session", default=None, help="固定板 + 双相机会话目录（锚定必需）")
    parser.add_argument("--factory", default=None, help="内参目录（默认 <root>/factory）")
    parser.add_argument("--wrist-intrinsics", default="right_wrist", help="腕相机内参文件名（<factory>/<它>.yaml）")
    parser.add_argument("--fixed-intrinsics", default="head", help="固定相机内参文件名")
    parser.add_argument("--bridge-fixed-key", default="head", help="双相机会话里固定相机的键名")
    parser.add_argument("--wrist-name", default=DEFAULT_WRIST_NAME, help="产物里腕相机的名字")
    parser.add_argument("--fixed-name", default=DEFAULT_FIXED_NAME, help="产物里固定相机的名字")
    parser.add_argument("--arm", default=DEFAULT_ARM, help="外部采集所在的臂（默认 right）")
    parser.add_argument("--world-arm", default=DEFAULT_WORLD_ARM, help="world 取哪条臂的基座（默认 left）")
    parser.add_argument("--mirror-axis", default="x", choices=("x", "y"), help="对称面法向取相机哪根轴")
    parser.add_argument("--plane", default="vertical", choices=("vertical", "camera"), help="对称面模式")
    parser.add_argument("--left-wrist-name", default="cam_left_wrist", help="左腕相机在产物里的名字")
    parser.add_argument(
        "--inherit-left-wrist",
        action="store_true",
        help="把右腕外参照搬给左腕（**同件同向装配假设**：只在两臂腕相机同支架同向装时成立；"
        "换支架 / 翻面装 / 装配角差几度都会偏——务必交叉验证或实测，见 inherit_wrist_camera）",
    )
    parser.add_argument("--out", default=None, help="产物输出路径（默认 frames.json）")
    parser.add_argument("--install", action="store_true", help=f"直接写产物（{FRAMES_RELATIVE}）")
    args = parser.parse_args(argv)

    wrist_path, fixed_path, factory = _resolve(args)
    wrist_session = ExternalSession.load(wrist_path)
    if args.arm and wrist_session.arm != args.arm:
        raise SystemExit(f"会话里的臂是 {wrist_session.arm!r}，与 --arm {args.arm!r} 不一致（改参数或换会话）")
    fixed_session = ExternalSession.load(fixed_path) if fixed_path is not None else None

    print(f"内参目录：{factory}")
    frames, _report = import_frames(
        wrist_session=wrist_session,
        wrist_intrinsics=load_intrinsics(factory / f"{args.wrist_intrinsics}.yaml"),
        fixed_session=fixed_session,
        fixed_intrinsics=None if fixed_session is None else load_intrinsics(factory / f"{args.fixed_intrinsics}.yaml"),
        wrist_name=args.wrist_name,
        fixed_name=args.fixed_name,
        fixed_key=args.bridge_fixed_key,
        world_arm=args.world_arm,
        mirror_axis=args.mirror_axis,
        plane=args.plane,
        log=print,
    )

    if args.inherit_left_wrist:
        print(inherit_wrist_camera(frames, source=args.wrist_name, target=args.left_wrist_name, arm="left"))

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


if __name__ == "__main__":
    raise SystemExit(main())
