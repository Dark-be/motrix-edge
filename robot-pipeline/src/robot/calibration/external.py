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

"""外部采集数据导入 + 「只采了一条臂」时的 ``world`` 锚定。

我们的 ``calibrate_extrinsics.py`` 采的是「角点 / 位姿 / 板参」三件套；别的工具（现场实录：
``piper_camera_calibration``）字段名不同但**信息等价**，而且通常**已经把角点检好了**。本模块
只做两件事：

1. **读**：``manifest.json`` + ``samples/*.json`` + ``factory/<相机>.yaml``（内参）→ 我们的
   :class:`~robot.calibration.board.BoardSpec`、内参字典、``(T_base_flange, T_cam_board)`` 列表。
   角点直接用样本里既有的 ``ids`` / ``corners_xy``，**不重新检测图像**（同一套 OpenCV ChArUco
   约定，实测逐点差 0.000 px；图像只在需要复核检测质量时才用）；
2. **锚定**：外部采集往往只覆盖**一条臂**（本次只有右臂），而 ``world`` 取左臂基座——左右基座的
   关系在这份数据里**没有观测**。:func:`symmetric_anchor` 用「左右臂相对 head 相机对称 + 两底座
   坐标系同向」把它推出来。**这是装配假设，不是标定结果。**

⚠️ 锚定精度（现场数据量化，详见 ``wiki/design/robot_pipeline_frames.md``「外部数据导入」）：

- 相机横轴相对水平倾斜 1.5° → 位置残差 ~17 mm（``plane="vertical"`` 按「同底板必同高」把它强制
  归零，本模块默认走这条）；
- 对称面若未过相机光心（横向偏 10 mm）→ 左基座偏 20 mm；对称面法向偏 1° → 左基座偏 ~11 mm；
- 「同向 / 绕竖直 180°」选错 → 偏差是**米级**，不是小误差。

⇒ 要 mm 级锚定必须实测：用左臂探针碰同一块板（``solve_probe`` → ``T_base_left_board``），与 head
相机的 ``T_head_board`` 合成。本模块不替你下结论，只把「假设从哪来」打印清楚。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from motrix_edge.geometry import (
    IDENTITY,
    MOUNT_FIXED,
    MOUNT_WRIST,
    WORLD_ALIAS,
    CameraExtrinsics,
    FrameSet,
    as_transform,
    invert_transform,
    matrix_to_rpy,
)
from robot.calibration.board import BoardSpec, pose_from_corners
from robot.calibration.solver import HandEyeResult, average_transforms, solve_hand_eye

#: 支持的外部采样会话类型（``manifest.json`` 的 ``session_type``）。
KIND_HAND_EYE = "handeye"
KIND_FIXED_BOARD_BRIDGE = "head_via_wrist"
KINDS = (KIND_HAND_EYE, KIND_FIXED_BOARD_BRIDGE)

#: 对称面法向取 head 相机的哪根轴（``x`` = 光学系横轴；``y`` = 图像上下方向）。
MIRROR_AXES = ("x", "y")

#: ``vertical``：法向投影到水平面（**保同高**，默认）；``camera``：原样用相机轴向（残留相机安装角）。
PLANES = ("vertical", "camera")

#: 现场数据里的键名与我们要产出的相机名（默认值即本次实采的那一套）。
DEFAULT_WRIST_KEY = "right_wrist"
DEFAULT_FIXED_KEY = "head"
DEFAULT_WRIST_NAME = "cam_right_wrist"
DEFAULT_FIXED_NAME = "cam_head"
DEFAULT_ARM = "right"
DEFAULT_WORLD_ARM = "left"

#: 产物 ``tool`` 字段取值——写明「这条链的锚定是外部数据 + 装配假设」，事后可追溯。
TOOL = "external-mirror"


class ExternalDataError(ValueError):
    """外部采集数据不可用（缺文件 / 字段缺失 / 会话类型不认识）——消息里带路径与字段名。"""


# ---- 读取 --------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    """读 JSON 并校验顶层是对象（异常统一收口成 :class:`ExternalDataError`）。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 外部文件什么样都可能，统一收口
        raise ExternalDataError(f"{path}: 不是合法 JSON（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ExternalDataError(f"{path}: 顶层必须是对象，实际 {type(payload).__name__}")
    return payload


def load_manifest(session: Path) -> dict:
    """读会话 ``manifest.json``（板参 / 臂 / 会话类型的**唯一来源**）。"""
    path = Path(session) / "manifest.json"
    if not path.exists():
        raise ExternalDataError(f"{path} 不存在（--wrist-session / --fixed-session 指到会话目录）")
    return _read_json(path)


def load_samples(session: Path) -> list[dict]:
    """读 ``samples/sample_*.json``（按文件名排序 = 采集顺序）。"""
    paths = sorted((Path(session) / "samples").glob("sample_*.json"))
    if not paths:
        raise ExternalDataError(f"{Path(session) / 'samples'} 里没有 sample_*.json")
    return [_read_json(path) for path in paths]


def load_intrinsics(path: Path) -> dict:
    """读外部内参文件（``factory/<相机>.yaml``）→ ``{fx, fy, cx, cy}``。

    ⚠️ 只取 ``intrinsics.camera_matrix``——外部文件里的 ``distortion`` 是**整流后**的值（现场实录
    全 0），与 :func:`~robot.calibration.board.pose_from_corners` 的「针孔 + 零畸变」一致。
    """
    path = Path(path)
    if not path.exists():
        raise ExternalDataError(f"内参文件 {path} 不存在")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block = dict(payload.get("intrinsics") or {})
    matrix = np.asarray(block.get("camera_matrix") or [], dtype=np.float64)
    if matrix.shape != (3, 3):
        raise ExternalDataError(f"{path}: intrinsics.camera_matrix 形状 {matrix.shape}（期望 3×3）")
    distortion = np.asarray(block.get("distortion") or [0.0] * 5, dtype=np.float64).reshape(-1)
    if distortion.size and float(np.max(np.abs(distortion))) > 1e-9:
        raise ExternalDataError(
            f"{path}: 畸变系数非零（{distortion.tolist()}）——本链只吃整流图（零畸变），请换整流后的内参文件"
        )
    if matrix[0, 0] <= 0.0 or matrix[1, 1] <= 0.0:
        raise ExternalDataError(f"{path}: 焦距非正（fx={matrix[0, 0]}, fy={matrix[1, 1]}）")
    return {"fx": float(matrix[0, 0]), "fy": float(matrix[1, 1]), "cx": float(matrix[0, 2]), "cy": float(matrix[1, 2])}


def board_spec_from_manifest(manifest: dict) -> BoardSpec:
    """``manifest.json`` 的 ``board`` 段 → :class:`BoardSpec`（**不套用我们的缺省板参**）。"""
    block = dict(manifest.get("board") or {})
    if str(block.get("board_type", "")) != "charuco":
        raise ExternalDataError(f"board.board_type={block.get('board_type')!r}（本链只支持 charuco）")
    missing = [key for key in ("squares_x", "squares_y", "square_length_m", "marker_length_m") if key not in block]
    if missing:
        raise ExternalDataError(f"manifest.board 缺字段 {missing}")
    return BoardSpec(
        squares_x=int(block["squares_x"]),
        squares_y=int(block["squares_y"]),
        square_length=float(block["square_length_m"]),
        marker_length=float(block["marker_length_m"]),
        dictionary=str(block.get("dictionary") or "DICT_5X5_50"),
    )


def _handeye_dict(sample: dict, key: str | None) -> dict:
    """取一条样本里的 ChArUco 块——两种会话布局不同，这里收口。"""
    if "cameras" in sample:  # head_via_wrist：``cameras.<key>.charuco``
        if not key:
            raise ExternalDataError(f"sample[{sample.get('index')}]: 双相机会话必须给 --fixed-key")
        block = dict((sample.get("cameras") or {}).get(key) or {})
        if not block:
            raise ExternalDataError(f"sample[{sample.get('index')}].cameras 里没有 {key!r}")
        return dict(block.get("charuco") or {})
    return dict(sample.get("charuco") or {})  # handeye：顶层 ``charuco``


@dataclass(frozen=True)
class ExternalSession:
    """一次外部采集会话（``manifest.json`` + ``samples/``）。"""

    path: Path
    kind: str
    arm: str
    board: BoardSpec
    samples: tuple[dict, ...]
    manifest: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> ExternalSession:
        """读会话目录；``session_type`` 不认识 / 字段缺失 → :class:`ExternalDataError`。"""
        path = Path(path)
        manifest = load_manifest(path)
        kind = str(manifest.get("session_type") or "")
        if kind not in KINDS:
            raise ExternalDataError(f"{path}/manifest.json: session_type={kind!r}（支持 {KINDS}）")
        arm = str((manifest.get("arm") or {}).get("side") or "")
        if not arm:
            raise ExternalDataError(f"{path}/manifest.json: arm.side 缺失（不知道该会话属于哪条臂）")
        return cls(
            path=path,
            kind=kind,
            arm=arm,
            board=board_spec_from_manifest(manifest),
            samples=tuple(load_samples(path)),
            manifest=manifest,
        )

    def charuco_of(self, sample: dict, key: str | None = None) -> tuple[np.ndarray, np.ndarray]:
        """一条样本里的 ``(corners (N,2), ids (N,))``。

        两种布局：手眼会话用顶层 ``charuco``（``key`` 不管）；固定板 + 双相机会话用
        ``cameras.<key>.charuco``。
        """
        block = _handeye_dict(sample, key)
        corners = np.asarray(block.get("corners_xy") or [], dtype=np.float64).reshape(-1, 2)
        ids = np.asarray(block.get("ids") or [], dtype=np.int64).reshape(-1)
        if corners.shape[0] != ids.shape[0] or corners.shape[0] == 0:
            raise ExternalDataError(f"sample[{sample.get('index')}]: corners_xy / ids 不匹配或为空")
        return corners, ids

    def flange_pose_of(self, sample: dict) -> np.ndarray:
        """一条样本的 ``T_base_flange``（外部字段名 ``robot.T_base_gripper``）。"""
        block = dict(sample.get("robot") or {})
        values = block.get("T_base_gripper")
        if values is None:
            raise ExternalDataError(f"sample[{sample.get('index')}].robot 缺 T_base_gripper")
        return as_transform(values, name=f"sample[{sample.get('index')}].T_base_gripper")

    def report(self) -> list[str]:
        board = self.board
        return [
            f"会话 {self.path}",
            f"  类型={self.kind} 臂={self.arm} 样本={len(self.samples)}",
            f"  板={board.squares_x}×{board.squares_y} 格 {board.square_length} m / 码 {board.marker_length} m"
            f" / {board.dictionary}",
        ]


# ---- 求解（用我们自己的链，不读对方的结果文件）--------------------------------


@dataclass(frozen=True)
class WristSolve:
    """腕相机手眼标定结果 + 判读量。"""

    result: HandEyeResult
    rms_px: tuple[float, ...]
    base_from_board: np.ndarray  # ``T_base_board``（逐帧平均；固定相机那一步要用它当公共基准）

    @property
    def rms_px_mean(self) -> float:
        return float(np.mean(self.rms_px)) if self.rms_px else float("nan")


@dataclass(frozen=True)
class FixedSolve:
    """固定相机（板静止）的板位姿平均 + 判读量。"""

    board_from_camera: np.ndarray  # ``T_head_board``（平均）
    rms_px_mean: float
    spread_deg: float
    spread_m: float
    count: int


def solve_wrist(session: ExternalSession, intrinsics: dict) -> WristSolve:
    """外部手眼样本 → 我们的 ``AX = XB`` 解 ``X = T_flange_cam`` + ``T_base_board``。

    ``T_base_board`` = 逐帧 ``T_base_flange · X · T_cam_board`` 的平均——**我们不读对方的
    ``T_base_target``**，自己算，才能把「对方的假设」也一起验掉。
    """
    if session.kind != KIND_HAND_EYE:
        raise ExternalDataError(f"{session.path}: session_type={session.kind!r} 不是手眼会话")
    poses, cameras, rms = [], [], []
    for sample in session.samples:
        corners, ids = session.charuco_of(sample)
        transform, error = pose_from_corners(corners, ids, session.board, intrinsics)
        poses.append(session.flange_pose_of(sample))
        cameras.append(transform)
        rms.append(error)
    if len(poses) < 2:
        raise ExternalDataError(f"{session.path}: 只有 {len(poses)} 个可用位姿，解不了手眼")
    result = solve_hand_eye(poses, cameras)
    boards = [poses[index] @ result.transform @ cameras[index] for index in range(len(poses))]
    base_from_board, _, _ = average_transforms(boards)
    return WristSolve(result=result, rms_px=tuple(rms), base_from_board=base_from_board)


def solve_fixed(session: ExternalSession, intrinsics: dict, key: str) -> FixedSolve:
    """固定相机（板**静止**）的外部样本 → 平均 ``T_cam_board``（``spread_*`` 是采样质量）。"""
    if session.kind != KIND_FIXED_BOARD_BRIDGE:
        raise ExternalDataError(f"{session.path}: session_type={session.kind!r} 不是固定板 + 双相机会话")
    views, rms = [], []
    for sample in session.samples:
        corners, ids = session.charuco_of(sample, key)
        transform, error = pose_from_corners(corners, ids, session.board, intrinsics)
        views.append(transform)
        rms.append(error)
    average, spread_deg, spread_m = average_transforms(views)
    return FixedSolve(
        board_from_camera=average,
        rms_px_mean=float(np.mean(rms)),
        spread_deg=spread_deg,
        spread_m=spread_m,
        count=len(views),
    )


# ---- 锚定 --------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """左臂锚定结果 + 它自己有多可信（都是**假设**的产物，不是残差）。"""

    world_from_base: np.ndarray  # ``T_world_base``（world = 左臂基座）
    arm: str
    separation_m: float
    height_mm: float
    tilt_deg: float  # 相机横轴相对水平面的夹角（``plane="camera"`` 时会漏进位置误差）
    axis: str
    plane: str

    def lines(self) -> list[str]:
        return [
            f"  锚定来源：**装配假设**（左右臂相对 {self.axis} 轴对称 + 两底座坐标系同向）",
            f"    对称面法向={self.axis} 模式={self.plane}"
            f" · 两基座间距 {self.separation_m * 1000:.0f} mm"
            f" · 高度差 {self.height_mm:+.0f} mm（同底板应 ≈ 0）"
            f" · 相机横轴相对水平倾斜 {self.tilt_deg:.2f}°",
            f"    T_world_base[{self.arm}]: 平移 {np.round(self.world_from_base[:3, 3], 4).tolist()} m"
            f" · 旋转 rpy {np.round(matrix_to_rpy(self.world_from_base[:3, :3]), 4).tolist()}",
        ]


def symmetric_anchor(
    base_from_camera: np.ndarray,
    *,
    arm: str = DEFAULT_ARM,
    axis: str = "x",
    plane: str = "vertical",
) -> Anchor:
    """由「head 相机看到的**一条**臂基座」推出另一条臂的基座（装配对称假设）。

    约定与公式（``world`` 取**对称的另一条臂**，即默认左臂）：

    - 两底座坐标系**同向**（只平移）→ ``T_world_base[arm]`` 的旋转是单位阵，信息全在平移；
    - 对称面过**相机光心**、法向取相机横轴（``axis``），平面 ``plane="vertical"`` 时把法向投影到
      水平面（满足「两臂同底板 ⇒ 基座同高」这条已知物理事实，消掉相机安装倾斜带来的 z 残差）。

    ``base_from_camera`` = ``T_base_flange_cam`` 那一类「基座 ← head 相机光学系」的变换（4×4）。
    """
    if axis not in MIRROR_AXES:
        raise ValueError(f"axis must be one of {MIRROR_AXES}, got {axis!r}")
    if plane not in PLANES:
        raise ValueError(f"plane must be one of {PLANES}, got {plane!r}")
    camera_from_base = invert_transform(as_transform(base_from_camera, name="T_base_cam"))
    # 相机横轴在**基座系**下的方向 = 相机姿态矩阵的 ``axis`` 列
    column = 0 if axis == "x" else 1
    lateral = np.asarray(camera_from_base[:3, column], dtype=np.float64)
    horizontal = np.array([lateral[0], lateral[1], 0.0], dtype=np.float64)
    horizontal_norm = float(np.linalg.norm(horizontal))
    if horizontal_norm < 1e-9:
        raise ValueError("相机的横轴与水平面垂直——这个安装姿态下推断不出对称面法向")
    # 倾斜度是**诊断量**：相机横轴离水平面越远，``plane="camera"`` 漏进位置误差越多（实测 1.5° → 17 mm）
    tilt = float(np.degrees(np.arccos(min(1.0, max(-1.0, horizontal_norm)))))
    normal = lateral if plane == "camera" else horizontal / horizontal_norm
    normal = normal / float(np.linalg.norm(normal))
    # 对称面：过相机光心、法向 normal → 基座原点的镜像位置 = 2·(光心在 normal 上的投影)·normal
    optical_center = np.asarray(camera_from_base[:3, 3], dtype=np.float64)
    left_origin = 2.0 * float(np.dot(optical_center, normal)) * normal
    world_from_base = np.eye(4, dtype=np.float64)  # 同向 → 旋转 = I
    world_from_base[:3, 3] = -left_origin  # 共享（平行）坐标系里：右基座原点 = -左基座位置
    separation = float(np.linalg.norm(left_origin))
    return Anchor(
        world_from_base=world_from_base,
        arm=arm,
        separation_m=separation,
        height_mm=float(left_origin[2] * 1000.0),
        tilt_deg=tilt,
        axis=axis,
        plane=plane,
    )


# ---- 组装产物 ----------------------------------------------------------------


@dataclass(frozen=True)
class ImportReport:
    """导入报告（脚本只负责打印，判读逻辑在这里，可离线测试）。"""

    wrist: WristSolve
    fixed: FixedSolve | None
    anchor: Anchor
    wrist_name: str
    fixed_name: str
    sessions: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        wrist = self.wrist
        out = [*self.sessions, "=== 腕相机（手眼，我们自己的解）==="]
        out.append(
            f"  {self.wrist_name}: {wrist.result.pairs} 对 · 姿态铺开 {wrist.result.rotation_spread_deg:.1f}°"
            f" · 残差 {wrist.result.rotation_rms_deg:.3f}° / {wrist.result.translation_rms_m * 1000:.2f} mm"
            f" · PnP RMS 均值 {wrist.rms_px_mean:.3f} px"
        )
        if self.fixed is not None:
            fixed = self.fixed
            out.append("=== 固定相机（板静止，多帧平均）===")
            out.append(
                f"  {self.fixed_name}: {fixed.count} 帧 · PnP RMS 均值 {fixed.rms_px_mean:.3f} px"
                f" · 板位姿帧间离散 {fixed.spread_deg:.3f}° / {fixed.spread_m * 1000:.2f} mm"
            )
        out.append("=== 左臂锚定 ===")
        out.extend(self.anchor.lines())
        out.append("  ⚠️ 这条锚定是**装配假设**，不是标定结果：量级 cm 且无法自证。")
        out.append("     要 mm 级请做左臂探针碰板实测（见 wiki/design/robot_pipeline_frames.md）。")
        return out


def import_frames(
    *,
    wrist_session: ExternalSession,
    wrist_intrinsics: dict,
    fixed_session: ExternalSession | None = None,
    fixed_intrinsics: dict | None = None,
    wrist_name: str = DEFAULT_WRIST_NAME,
    fixed_name: str = DEFAULT_FIXED_NAME,
    fixed_key: str = DEFAULT_FIXED_KEY,
    world_arm: str = DEFAULT_WORLD_ARM,
    mirror_axis: str = "x",
    plane: str = "vertical",
    log: Callable[[str], None] | None = None,
) -> tuple[FrameSet, ImportReport]:
    """外部采集 → :class:`FrameSet`（``world`` = 左臂基座）+ 报告。

    产物里：``arms[world_arm] = I``、``arms[wrist_session.arm] = 锚定值``、
    ``cameras[fixed_name]``（固定相机，``T_world_cam``）、``cameras[wrist_name]``（腕相机，
    ``T_flange_cam``）；``probe_tip`` **不给**（外部数据没有探针触碰）。
    """
    say = log or (lambda _message: None)
    wrist = solve_wrist(wrist_session, wrist_intrinsics)
    arm = wrist_session.arm
    if arm == world_arm:
        raise ExternalDataError(f"腕相机所在臂（{arm}）不能同时是 world 基准臂（{world_arm}）——换个臂或换 world")

    base_from_board = wrist.base_from_board
    fixed: FixedSolve | None = None
    base_from_fixed: np.ndarray | None = None
    if fixed_session is not None:
        if fixed_intrinsics is None:
            raise ExternalDataError("给了固定板会话就必须给它的内参（--fixed-intrinsics）")
        fixed = solve_fixed(fixed_session, fixed_intrinsics, fixed_key)
        base_from_fixed = base_from_board @ invert_transform(fixed.board_from_camera)

    anchor_source = base_from_fixed if base_from_fixed is not None else None
    if anchor_source is None:
        raise ExternalDataError(
            "锚定需要 head 相机那一段（--fixed-session）：它把「基座 ↔ 世界」接起来；只有手眼会话时无法定位相机基座"
        )
    anchor = symmetric_anchor(anchor_source, arm=arm, axis=mirror_axis, plane=plane)

    cameras: dict[str, CameraExtrinsics] = {}
    if fixed is not None:
        cameras[fixed_name] = CameraExtrinsics(
            name=fixed_name,
            mount=MOUNT_FIXED,
            arm=None,
            transform=anchor.world_from_base @ base_from_fixed,
            rms_m=fixed.spread_m,
        )
    cameras[wrist_name] = CameraExtrinsics(
        name=wrist_name,
        mount=MOUNT_WRIST,
        arm=arm,
        transform=wrist.result.transform,
        rms_m=wrist.result.translation_rms_m,
    )

    frames = FrameSet(
        world=WORLD_ALIAS,
        arms={world_arm: IDENTITY.copy(), arm: anchor.world_from_base},
        cameras=cameras,
        probe_tips=None,
        calibrated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        tool=TOOL,
    )
    report = ImportReport(
        wrist=wrist,
        fixed=fixed,
        anchor=anchor,
        wrist_name=wrist_name,
        fixed_name=fixed_name,
        sessions=[*wrist_session.report(), *(fixed_session.report() if fixed_session is not None else [])],
    )
    for line in report.lines():
        say(line)
    return frames, report


# ---- 左腕相机：从右腕**继承**外参（同件同向假设）-------------------------------


def inherit_wrist_camera(
    frames: FrameSet,
    *,
    source: str,
    target: str,
    arm: str,
    note: str | None = None,
) -> str:
    """把 ``source``（已标定的腕相机）的外参**照搬**给 ``target``（另一条臂的腕相机）。

    ⚠️ **这是装配假设，不是标定**：只有在「两条臂的腕相机是**同一支架件、相对各自法兰同向安装**」
    时才成立（法兰系是**随臂的局部系**，所以与两臂怎么摆、底座是否镜像无关）。两种常见情形会破：

    - 支架是**镜像件**（或同一件翻面装）→ 相差一个轴向翻转（外参会整体镜像，坐标错到不能用）；
    - 同件但**装配角度差几度** → 误差与右腕同级偏大（几度 / 几 cm），叠加在继承值上。

    **怎么证伪（现场 1 分钟）**：把腕相机对着同一场景（例如夹爪 + 一块板），比对左右两路原图——
    两图**朝向一致** ⇒ 同向；两图**互为镜像** ⇒ 镜像件。或者做一次交叉验证：让同一个物理点同时出现在
    两路画面里，用 ``/v1/depth`` 各查一次 ``xyz_world``，两点应在 mm–cm 量级内一致（差到米级 / 方向
    翻转 ⇒ 假设破了）。

    **怎么实测（10 分钟，推荐做一次）**：板不动，head 相机与左腕相机**同拍**，左臂摆 3–6 个位姿 →
    ``T_flange_board = FK_left⁻¹ · T_base_left_board``，逐帧 ``X = T_flange_board · T_board_cam``
    取平均（不需要 ``AX = XB``）；``T_base_left_board = T_base_left_head · T_head_board``。

    继承出的相机**不写 ``rms_m``**（没有实测残差，宁缺勿假），并返回一行说明供脚本打印 / 记录。
    """
    if source == target:
        raise ExternalDataError(f"inherit_wrist_camera: source 与 target 同名（{source!r}）")
    origin = frames.cameras.get(str(source))
    if origin is None or origin.mount != MOUNT_WRIST:
        raise ExternalDataError(f"inherit_wrist_camera: {source!r} 不是已标定的腕相机（无法继承）")
    if origin.arm == arm:
        raise ExternalDataError(f"inherit_wrist_camera: {source!r} 已经挂在臂 {arm!r} 上——继承的目标应是**另一条臂**")
    frames.cameras[str(target)] = CameraExtrinsics(
        name=str(target),
        mount=MOUNT_WRIST,
        arm=str(arm),
        transform=origin.transform.copy(),
        rms_m=None,  # 继承值没有实测残差
    )
    return note or (
        f"  相机 {target}: wrist({arm}) · T_flange_cam **继承自 {source}**（同件同向假设，"
        f"无实测残差）——换支架 / 翻面装 / 装配角差几度都会偏，务必用交叉验证或 head 当桥实测复核"
    )


__all__ = [
    "DEFAULT_ARM",
    "DEFAULT_FIXED_KEY",
    "DEFAULT_FIXED_NAME",
    "DEFAULT_WORLD_ARM",
    "DEFAULT_WRIST_KEY",
    "DEFAULT_WRIST_NAME",
    "KINDS",
    "KIND_FIXED_BOARD_BRIDGE",
    "KIND_HAND_EYE",
    "MIRROR_AXES",
    "PLANES",
    "TOOL",
    "Anchor",
    "ExternalDataError",
    "ExternalSession",
    "FixedSolve",
    "ImportReport",
    "WristSolve",
    "board_spec_from_manifest",
    "import_frames",
    "inherit_wrist_camera",
    "load_intrinsics",
    "load_manifest",
    "load_samples",
    "solve_fixed",
    "solve_wrist",
    "symmetric_anchor",
]
