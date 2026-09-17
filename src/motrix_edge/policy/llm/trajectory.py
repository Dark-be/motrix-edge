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

"""LLM 轨迹的解析 / 校验 / 重采样（设计见 wiki/design/motrix_edge_llm_policy.md）。

模型返回的**稀疏笛卡尔轨迹点**（waypoint + 时间）经本模块变成控制步上的密集动作块
``[H, dim]``：

- ``parse_trajectory``：把模型输出（JSON 文本 / dict）解析成 ``TrajectoryPoint`` 列表，
  **不可信输入全部校验**（维度 / 类型 / 臂名 / 时间单调 / 有限值 / 点数）；
- ``resample_trajectory``：按控制步时间轴重采样——位置 / 姿态线性插值、夹爪分段常数，
  再按 ``max_pose_step`` 限制相邻步最大笛卡尔位移。

布局：动作块按臂分段（顺序 = 传入的 ``arms``），每臂 7 维 = ``pos(3) + rot(3) + gripper(1)``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np

from motrix_edge.utils.data_handler import debug_print

# 每臂动作布局：pos(3) + rot(3) + gripper(1)（与 ActionSpace.CARTESIAN_POSE 契约一致）
POSE_DIM = 6
ACTION_DIM_PER_ARM = 7
DEFAULT_MAX_POINTS = 32  # 轨迹点数上限（超出截断到最早的 N 个点）

# 轨迹点字段名（模型输出契约，单点定义）
KEY_TRAJECTORY = "trajectory"
KEY_T = "t"
KEY_ARM = "arm"
KEY_POS = "pos"
KEY_ROT = "rot"
KEY_GRIPPER = "gripper"


class TrajectoryError(ValueError):
    """轨迹非法（模型输出不可信）：调用方据此**丢弃整块**、本步不下发（见设计文档失败策略）。"""


@dataclass
class TrajectoryPoint:
    """一个轨迹点：``t`` 秒（相对本块起点）、末端位置（米）、姿态（弧度）、夹爪开合 ``[0, 1]``。

    ``arm`` = 该点所属臂（多臂布局必填；单臂布局可省略）。``rot`` / ``gripper`` 缺省时
    由解析阶段**继承上一点**（模型可只给变化量）。
    """

    t: float
    pos: np.ndarray
    rot: np.ndarray
    gripper: float
    arm: str | None = None

    def vector(self) -> np.ndarray:
        """展平成每臂动作向量 ``[7]`` = pos(3) + rot(3) + gripper(1)。"""
        return np.concatenate([self.pos, self.rot, [self.gripper]]).astype(np.float64)


def extract_json_object(payload) -> dict:
    """从模型输出里取出 JSON 对象（容忍 ``` 包裹与前后解释文字）。

    dict 直接透传；字符串先按整体 JSON 解析，失败则扫描首个可解析的 ``{...}``（``raw_decode``）。
    取不到对象 → ``TrajectoryError``。
    """
    if isinstance(payload, dict):
        return payload
    text = str(payload or "").strip()
    if not text:
        raise TrajectoryError("empty model response")
    text = text.strip("`").strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):  # 扫描首个可解析对象（跳过解说文字）
        if char != "{":
            continue
        try:
            loaded, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            return loaded
    raise TrajectoryError("no JSON object found in model response")


def _as_float(value, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TrajectoryError(f"{what} must be a number, got {value!r}") from exc
    if not np.isfinite(number):
        raise TrajectoryError(f"{what} must be finite, got {value!r}")
    return number


def _as_vector(value, length: int, what: str) -> np.ndarray:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, np.ndarray)):
        raise TrajectoryError(f"{what} must be a list of {length} numbers, got {value!r}")
    if len(value) != length:
        raise TrajectoryError(f"{what} must have {length} elements, got {len(value)}")
    return np.asarray([_as_float(v, f"{what}[{i}]") for i, v in enumerate(value)], dtype=np.float64)


def parse_trajectory(payload, arms, max_points: int = DEFAULT_MAX_POINTS) -> list[TrajectoryPoint]:
    """解析并校验模型轨迹（见设计文档「轨迹协议」）。

    - ``payload``：模型输出（JSON 文本 / 已解析 dict）；
    - ``arms``：启用臂名（物理顺序）；**多臂布局下每点必须带合法 ``arm``**（缺失 → 非法，
      避免「未标臂」被误解为「两臂同时动」）；单臂布局可省略；
    - ``max_points``：点数上限（超出截断到最早的 N 个点）。

    非法 → ``TrajectoryError``（调用方丢弃整块、本步不下发）。
    """
    arms = [str(a) for a in (arms or [])]
    if not arms:
        raise TrajectoryError("no enabled arm to interpret the trajectory")
    body = extract_json_object(payload)
    raw_points = body.get(KEY_TRAJECTORY)
    if not isinstance(raw_points, list) or not raw_points:
        raise TrajectoryError(f"'{KEY_TRAJECTORY}' must be a non-empty list")
    default_arm = arms[0] if len(arms) == 1 else None

    points: list[TrajectoryPoint] = []
    prev_rot: np.ndarray | None = None
    prev_gripper: float | None = None
    prev_t = 0.0
    limit = int(max_points)
    if len(raw_points) > limit:  # 模型给的点数超上限：截断到最早的 N 个（尾部丢弃）——告警
        debug_print(
            "LLMTrajectory",
            f"model returned {len(raw_points)} waypoints; truncated to max_points={limit} "
            "(raise 'max_points' if the tail matters)",
            "WARNING",
        )
    for index, raw in enumerate(raw_points[:limit]):
        if not isinstance(raw, dict):
            raise TrajectoryError(f"trajectory[{index}] must be an object")
        arm = raw.get(KEY_ARM)
        if arm is None:
            arm = default_arm
            if arm is None:
                raise TrajectoryError(f"trajectory[{index}] must declare '{KEY_ARM}' (multi-arm layout)")
        else:
            arm = str(arm).strip().lower()
            if arm not in [a.lower() for a in arms]:
                raise TrajectoryError(f"trajectory[{index}] unknown arm {arm!r} (available: {arms})")
            arm = arms[[a.lower() for a in arms].index(arm)]
        t = _as_float(raw[KEY_T], f"trajectory[{index}].{KEY_T}") if raw.get(KEY_T) is not None else prev_t
        if index and t < prev_t:
            raise TrajectoryError(f"trajectory[{index}].{KEY_T} must be non-decreasing ({t} < {prev_t})")
        pos = _as_vector(raw.get(KEY_POS), 3, f"trajectory[{index}].{KEY_POS}")
        rot = (
            _as_vector(raw[KEY_ROT], 3, f"trajectory[{index}].{KEY_ROT}")
            if raw.get(KEY_ROT) is not None
            else (prev_rot if prev_rot is not None else np.zeros(3, dtype=np.float64))
        )
        if raw.get(KEY_GRIPPER) is not None:
            gripper = float(np.clip(_as_float(raw[KEY_GRIPPER], f"trajectory[{index}].{KEY_GRIPPER}"), 0.0, 1.0))
        else:
            gripper = prev_gripper if prev_gripper is not None else 0.0
        points.append(TrajectoryPoint(t=t, pos=pos, rot=rot, gripper=gripper, arm=arm))
        prev_rot, prev_gripper, prev_t = rot, gripper, t

    if not points:
        raise TrajectoryError("trajectory is empty after validation")
    return points


def _interp_arm(ts: np.ndarray, arm_points: list[TrajectoryPoint]) -> np.ndarray:
    """单臂重采样：位置 / 姿态线性插值，夹爪取「最近一个已到达的点」的值（分段常数）。"""
    times = np.asarray([p.t for p in arm_points], dtype=np.float64)
    # 同一时刻多点：保留最后一个（时间轴需严格递增才能插值）
    keep = np.concatenate([times[1:] > times[:-1], [True]])
    times = times[keep]
    values = np.stack([p.vector() for p in arm_points], axis=0)[keep]  # [N, 7]
    if len(times) == 1 or times[-1] <= 0:
        return np.repeat(values[-1:], len(ts), axis=0)
    pose = np.stack([np.interp(ts, times, values[:, col]) for col in range(POSE_DIM)], axis=1)  # [H, 6]
    index = np.clip(np.searchsorted(times, ts, side="right") - 1, 0, len(times) - 1)
    gripper = values[index, POSE_DIM][:, None]  # 夹爪不插值（开关型执行器）
    return np.concatenate([pose, gripper], axis=1)


def resample_trajectory(
    points: list[TrajectoryPoint],
    horizon: int,
    arms,
    hold=None,
    max_pose_step: float = 0.0,
) -> np.ndarray:
    """稀疏轨迹点 → 控制步密集动作块 ``[H, dim]``（dim = 7 × 臂数，按 ``arms`` 顺序分段）。

    - ``horizon``：块长 H（步）；时间轴在 ``[0, 轨迹末点 t]`` 上均匀取 H 个样本——即轨迹点的
      ``t`` 决定**点间相对时长**，整块被拉伸 / 压缩到 H 步（实际执行时长 = H / infer_freq）；
    - ``hold``：``{臂名: [7] 动作向量}``，该臂**整条轨迹未出现**时的保持值（通常传当前位姿 +
      当前夹爪，避免未动的臂被拉回零位）；
    - ``max_pose_step``：相邻输出步最大笛卡尔位移（米，> 0 时生效）；超出则**整块按比例缩放**
      （以块首点为锚，保持轨迹形状与终点方向），防止模型给出跨工作空间的跳变指令。
    """
    arms = [str(a) for a in (arms or [])]
    if not arms:
        raise TrajectoryError("no enabled arm to resample the trajectory onto")
    if not points:
        raise TrajectoryError("trajectory is empty")
    horizon = max(1, int(horizon))
    dim = ACTION_DIM_PER_ARM * len(arms)
    hold = hold or {}

    duration = float(max(p.t for p in points))
    ts = np.linspace(0.0, duration, horizon) if duration > 0 else np.zeros(horizon, dtype=np.float64)

    block = np.zeros((horizon, dim), dtype=np.float64)
    for index, arm in enumerate(arms):
        arm_points = [p for p in points if p.arm == arm]
        if arm_points:
            segment = _interp_arm(ts, arm_points)
        else:  # 该臂整条轨迹未出现 → 保持（未提供保持值则用零位）
            keep = np.asarray(hold.get(arm, np.zeros(ACTION_DIM_PER_ARM, dtype=np.float64)), dtype=np.float64)
            if keep.shape[0] != ACTION_DIM_PER_ARM:
                raise TrajectoryError(f"hold[{arm!r}] must have {ACTION_DIM_PER_ARM} elements")
            segment = np.repeat(keep.reshape(1, -1), horizon, axis=0)
        block[:, index * ACTION_DIM_PER_ARM : (index + 1) * ACTION_DIM_PER_ARM] = segment

    if max_pose_step and float(max_pose_step) > 0 and horizon > 1:
        pose_cols = [
            c
            for index in range(len(arms))
            for c in range(index * ACTION_DIM_PER_ARM, index * ACTION_DIM_PER_ARM + POSE_DIM)
        ]
        pose = block[:, pose_cols]
        span = float(np.max(np.abs(np.diff(pose, axis=0)))) if horizon > 1 else 0.0
        limit = float(max_pose_step)
        if span > limit:
            block[:, pose_cols] = pose[0] + (pose - pose[0]) * (limit / span)
    return block


def hold_from_observation(pose: np.ndarray | None, qpos: np.ndarray | None, arms) -> dict:
    """从观测构造「保持值」：``{臂名: [7]}``（位姿 + 夹爪）。

    ``pose`` = ``observations/pose``（每臂 6 维，物理顺序同 ``arms``）；夹爪取 ``qpos`` 每臂
    最后一维（关节布局：每臂 6 关节 + 夹爪）。缺失的臂不给保持值（重采样时按零位处理）。
    """
    arms = [str(a) for a in (arms or [])]
    hold: dict[str, np.ndarray] = {}
    if pose is None:
        return hold
    pose = np.asarray(pose, dtype=np.float64).reshape(-1)
    for index, arm in enumerate(arms):
        start = index * POSE_DIM
        if pose.shape[0] < start + POSE_DIM:
            break
        gripper = 0.0
        if qpos is not None:
            flat = np.asarray(qpos, dtype=np.float64).reshape(-1)
            last = index * ACTION_DIM_PER_ARM + ACTION_DIM_PER_ARM - 1
            if flat.shape[0] > last:
                gripper = float(np.clip(flat[last], 0.0, 1.0))
        hold[arm] = np.concatenate([pose[start : start + POSE_DIM], [gripper]])
    return hold


def trajectory_block(
    payload,
    arms,
    horizon: int,
    hold=None,
    max_points: int = DEFAULT_MAX_POINTS,
    max_pose_step: float = 0.0,
) -> np.ndarray:
    """``parse_trajectory`` + ``resample_trajectory`` 的组合入口（模型输出 → 动作块）。"""
    points = parse_trajectory(payload, arms=arms, max_points=max_points)
    return resample_trajectory(points, horizon=horizon, arms=arms, hold=hold, max_pose_step=max_pose_step)
