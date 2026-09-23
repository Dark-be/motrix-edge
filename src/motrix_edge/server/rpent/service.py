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

"""rpent.service —— ``POST /call`` 的服务实现（RPent 的 ``env.*`` 方法转发到 edge 既有路径）。

协议 / 方法映射 / 租约 / dry-run / 布局转换的设计说明见包文档 :mod:`motrix_edge.server.rpent`；
到位等待（settle）参数与回执见 :mod:`~motrix_edge.server.rpent.settle`。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import numpy as np

from motrix_edge.adapter.base import (
    CAMERA_PREFIX,
    KEY_ACTION,
    KEY_GRIPPER,
    KEY_POSE,
    KEY_POSE_TARGET,
    KEY_QPOS,
    ActionSpace,
)
from motrix_edge.command import SOURCE_RPENT, CommandError
from motrix_edge.lease import LeaseError, LeaseManager, LeaseState
from motrix_edge.utils.data_handler import debug_print

from .coerce import (
    as_action_matrix,
    as_float_array,
    decode_jpeg,
    seconds_until,
    space_value,
    split_arm_and_vector,
    wrap_angles,
)
from .errors import RpentError
from .layout import (
    CARTESIAN_ACTION_DIM_PER_ARM,
    layout_block_dim,
    layout_frame_dim,
    matrix_to_quat,
    matrix_to_rpy,
    resolve_layout,
    rot6d_to_matrix,
    rpent_gripper_to_edge,
    rpy_to_matrix,
)
from .node_view import EdgeNodeView
from .settle import SettleConfig, unreached, wait_for_reached

# ---- 服务 --------------------------------------------------------------------


class RpentService:
    """``POST /call`` 的控制器：把 RPent 的 ``env.*`` 方法转发到 edge 既有路径。

    ``node``：正在运行的 ``EdgeNode``——本服务只经
    :class:`~motrix_edge.server.rpent.node_view.EdgeNodeView` 读它（adapter / 观测缓存 / 实测频率 /
    会话），**写路径一律走 ``CommandService``**。
    ``commands``：``CommandService``（``env.reset`` 走命令通道；缺省 None → 该方法报不支持）。
    ``leases``：Edge 级 ``LeaseManager``（校验与取当前活跃租约）。
    ``base_cfg``：读 ``server.rpent`` 配置段（``lease_id`` 固定租约 / ``step_hz`` 动作节奏）。
    """

    # 免租约方法：自描述 / 握手，无副作用、不含图像——让 agent 在签发租约前就能协商能力
    LEASE_FREE_METHODS = frozenset(
        {"healthz", "session.register", "session.close", "env.get_env_meta", "env.get_camera_meta"}
    )

    def __init__(
        self,
        node,
        commands=None,
        leases: LeaseManager | None = None,
        *,
        base_cfg: dict | None = None,
        lease_id: str | None = None,
        step_hz: float | None = None,
    ):
        self._view = EdgeNodeView(node)
        self._commands = commands
        self._leases = leases or LeaseManager()
        cfg = dict(((base_cfg or {}).get("server") or {}).get("rpent") or {})
        self._pinned_lease_id = lease_id if lease_id is not None else cfg.get("lease_id")
        self._step_hz = step_hz if step_hz is not None else cfg.get("step_hz")
        self._action_layout = cfg.get("action_layout")
        self._dry_run = bool(cfg.get("dry_run", False))
        # 观测图来源：native = 直读 adapter 原图（RPent 内联给模型，小物体要看得清）；
        # preview = 用 FrameManager 的降采样缓存（320x240，省带宽）。
        self._image_source = str(cfg.get("image_source") or "native").strip().lower()
        settle = dict(cfg.get("settle") or {})
        # 到位容差：**按 MIT 实际稳态误差标定**（底层只有 P/D、**无重力 / 力矩前馈**，容差小于
        # 稳态误差时 `reached` 永远不成立）——缺省值偏松，真机现场按实测收敛；参数与回执见
        # ``rpent/settle.py``。
        self._settle_config = SettleConfig.from_mapping(settle)
        self._handlers = {
            "healthz": self._healthz,
            "session.register": self._session_noop,
            "session.close": self._session_noop,
            "env.get_env_meta": self._get_env_meta,
            "env.get_camera_meta": self._get_camera_meta,
            "env.get_observation": self._get_observation,
            "env.get_robot_state": self._get_robot_state,
            "env.get_task_language": self._get_task_language,
            "env.reset": self._reset,
            "env.move_delta": self._move_delta,
            "env.rotate_delta": self._rotate_delta,
            "env.set_gripper": self._set_gripper,
            "env.recover_joint_posture": self._recover_joint_posture,
            "env.step": self._step,
            "env.chunk_step": self._chunk_step,
        }

    # ---- 入口 ---------------------------------------------------------------

    @property
    def methods(self) -> list[str]:
        """已实现的方法名（能力自省 / ``GET /v1/rpent``）。"""
        return sorted(self._handlers)

    def call(self, method: str, args: tuple = (), kwargs: dict | None = None, session_id: str | None = None) -> Any:
        """分派一次 RPC 调用；失败抛 :class:`RpentError`（由 HTTP 层转 ``ok=false`` 信封）。

        ``session_id`` 仅用于日志：edge 的授权载体是 Edge 级租约，不是 RPC 会话。
        """
        handler = self._handlers.get(str(method))
        if handler is None:
            raise RpentError(f"unknown RPC method: {method!r}", kind="unknown_method")
        # ``session_id`` 只进日志：edge 的授权载体是 Edge 级租约，不是 RPC 会话（多客户端排障用）
        debug_print("rpent", f"{method} [session={session_id or '-'}]", "DEBUG")
        if method not in self.LEASE_FREE_METHODS:
            self._require_lease()
        return handler(*tuple(args or ()), **dict(kwargs or {}))

    # ---- 租约 ---------------------------------------------------------------

    def lease_status(self) -> dict:
        """租约解析结果（``GET /v1/rpent`` / ``env.get_env_meta`` 展示与排障用）。

        ``satisfied`` 的语义是**租约现在可用**（能通过 ``require``），不是“解析出了 id”：
        配了 pinned ``lease_id`` 但该租约未安装 / 未生效 / 已过期时，``satisfied`` 为 ``false``
        并给出 ``reason``（否则会在调用时才报 ``kind=lease``，白跑一轮）。
        另带 ``expires_at`` / ``expires_in_s``：RPent 连上就 ``env.reset``，启动阶段用它们
        自检「有没有租约」「租约能不能盖住整轮 run」（缺口 → 中途变 ``kind=lease``）。
        """
        lease_id = self._resolve_lease_id()
        info = self._leases.status() or {}
        matches = lease_id is not None and info.get("lease_id") == lease_id
        state = info.get("state") if matches else None
        usable = matches and state == LeaseState.ACTIVE.value
        expires_at = info.get("expires_at") if matches else None
        return {
            "lease_id": lease_id,
            "source": "pinned" if self._pinned_lease_id else ("active" if lease_id else None),
            "satisfied": bool(usable),
            "required": True,  # 写方法与 env.reset 都要租约（自描述方法免）
            "state": state,
            "expires_at": expires_at,
            "expires_in_s": seconds_until(expires_at),
            "reason": None
            if usable
            else (
                f"resolved lease {lease_id!r} is not installed / not active (state={state})"
                if lease_id
                else "no Edge lease installed (no active lease)"
            ),
        }

    def settle_status(self) -> dict:
        """到位等待与下发模式自描述（回执里的 ``reached`` 靠它；``dry_run`` 让 agent 启动就能告警）。"""
        return {
            "enabled": self._settle_config.enabled,
            "pos_tol_m": self._settle_config.pos_tol,
            "rot_tol_rad": self._settle_config.rot_tol,
            "timeout_s": self._settle_config.timeout_s,
            "max_timeout_s": self._settle_config.max_timeout_s,
            "target_wait_s": self._settle_config.target_wait_s,
            "image_source": self._image_source,
            "dry_run": self._dry_run,
            "action_layout": self._action_layout or None,
        }

    def _resolve_lease_id(self) -> str | None:
        """写 / 读方法的租约 id：配置固定租约优先，否则取当前活跃租约。"""
        if self._pinned_lease_id:
            return str(self._pinned_lease_id)
        return self._leases.status().get("lease_id")

    def _require_lease(self) -> str | None:
        """校验租约（与 ``/v1/commands`` / ``/v1/preview`` 同规则）：无活跃租约即拒绝。"""
        lease_id = self._resolve_lease_id()
        try:
            self._leases.require(lease_id)
        except LeaseError as exc:  # 租约层唯一异常：缺失 / 不匹配 / 过期 / 已撤销
            raise RpentError(
                f"{exc} (no active Edge lease for the RPent face; install a lease first or pin server.rpent.lease_id)",
                kind="lease",
            ) from exc
        return lease_id

    # ---- 自描述 -------------------------------------------------------------

    def _healthz(self) -> dict:
        """存活探针（RPent 连接前轮询它）；免租约。"""
        return {"status": "ok"}

    def _session_noop(self, *args, **kwargs) -> dict:
        """RPent 的 session RPC（按客户端隔离策略状态用）：edge 的隔离载体是 Edge 级租约，
        故这里只确认收到，不做状态管理。"""
        return {"ok": True}

    def _get_env_meta(self) -> dict:
        """环境自描述：能力 / 动作空间 / 臂 / 相机 / 观测布局（RPent 用于工具协商）。

        ``explicit_reset_only: true`` 是 RPent 的硬要求（agent 显式复位，env 不在连接时偷偷复位）
        ——edge 本来就不隐式复位（``reset`` 只设 home 目标），故直接声明。
        ``lease`` / ``settle`` 让 agent 在启动阶段就知道“写操作会不会被拒”“原语会不会阻塞到到位”。
        """
        view = self._view
        arms = view.arms
        pose_dim = view.dim_per_arm(ActionSpace.POSE)
        joint_per_arm = self._rpent_dim_per_arm(ActionSpace.JOINT)
        return {
            "ok": True,
            "edge": {"adapter_name": view.adapter_name, "adapter_type": view.adapter_type, "node_state": view.state},
            "explicit_reset_only": True,
            "action_dim": joint_per_arm * len(arms),
            "action_space": ActionSpace.JOINT.value,
            "action_spaces": [space_value(space) for space in view.action_spaces],
            # RPent 侧每臂 7 维 = 值（joint / pose 各 6）+ 夹爪 1（edge 内部两个独立空间，桥接层拼/拆）
            "action_dim_per_arm": joint_per_arm,
            "cartesian_dim_per_arm": CARTESIAN_ACTION_DIM_PER_ARM,
            "pose_dim_per_arm": pose_dim,
            # 位姿约定：读（``observations/pose``）与写（``pose`` 动作）同系，均为 `xyz + rpy`
            "pose_convention": "xyz_rpy" if pose_dim else None,
            # 读（位姿值）与写（pose 动作）必须同系；机器人侧用哪个系由它自己声明
            "pose_frame": view.pose_frame,
            "gripper_range": [0.0, 1.0],  # 0 = 闭合，1 = 张开（独立 gripper 空间，每臂 1 维）
            "arms": arms,
            "all_arms": view.all_arms,
            "observation_keys": [KEY_QPOS, KEY_GRIPPER, f"{CAMERA_PREFIX}<camera>"],
            "call_endpoint": "/call",
            "lease": self.lease_status(),
            "settle": self.settle_status(),
            **self._camera_payload(),
        }

    def _camera_payload(self) -> dict:
        """相机段（``cameras`` / ``agent_observation`` / 观测映射）—— ``get_env_meta`` 与
        ``get_camera_meta`` **共用同一实现**：RPent 的 ``dump_state`` 只读 ``get_camera_meta``
        来决定内联哪几路图，两处一旦不一致就会变成“只给路径不给图”（模型盲跑）。
        """
        view = self._view
        images = view.images()
        camera_names = view.cameras
        preview_size = view.image_size()
        # 相机名必须是合法的 artifact 基名（RPent 的 ``EnvState._validate_name`` 拒 ``.`` / ``/``）：
        # ``raw_camera_frames`` 的 key 会被直接当成 PNG 文件名，而 ``inline_cameras`` 必须与之同名。
        native = {name: list(images.get(name) or preview_size) for name in camera_names}
        return {
            "cameras": native,  # 相机原生分辨率（观测图按它给原图）
            "enabled_cameras": camera_names,
            "image_size": list(preview_size),  # FrameManager 预览缓存尺寸（WebRTC）
            "observation_image_size": list(next(iter(native.values()), preview_size)),
            "image_source": self._image_source,
            # RPent 的 ``agent_observation``：哪几路相机进模型上下文（其余只落盘，省 token）
            "agent_observation": {
                "inline_cameras": camera_names[:4],  # RPent 只认 4 个 inline 字节键
                "auxiliary_cameras": camera_names[4:],
            },
            "observation_camera_map": {},  # 我方帧按相机名直出，无别名映射
            "projection_views": {},
        }

    def _get_camera_meta(self) -> dict:
        """相机元信息（RPent 用于解析观测键 / 落盘命名 / **内联哪几路图**）；免租约。

        RPent 的 ``dump_state`` 把本返回值存在该 step 的 ``camera_meta.json``，``view_camera_meta``
        再从文件里读；内联相机名必须与落盘 PNG 基名一致（不能带 ``.png`` 或路径）。
        """
        return {
            "ok": True,
            "encoding": "uint8 HWC (RGB)",  # 解码自观测 JPEG；RPent 侧可直接写 PNG
            **self._camera_payload(),
        }

    # ---- 观测 ---------------------------------------------------------------

    def _get_observation(self) -> dict:
        """当前观测：``states``（同帧 qpos）+ qpos / pose + 全部启用相机的 uint8 帧。

        RPent 的 ``base`` client 要求返回里带 ``states``（缓存为 ``wrapped_state_vector``）；
        ``raw_camera_frames`` 按相机名直出（RPent 的落盘循环按名字写 ``<camera>.png``）。
        图像**必须是 ndarray**（RPent 侧 ``np.asarray`` 后写 PNG），不接受 JPEG bytes；
        默认给**原图**（相机原生分辨率）。
        """
        return self._observation_payload()

    def _get_robot_state(self) -> dict:
        """机器人状态快照：扁平 qpos / pose / 每臂夹爪 + **每臂状态块** + ``wrapped_state_vector``。

        每臂块（``left_arm`` / ``right_arm``）是 RPent 机器人包的期望形态（其 VLA 技能与健康
        判定读它）：``arm_joint_position``（每臂 qpos 段，含夹爪）、``gripper_open``、
        ``tcp_pose`` = ``[x, y, z, qx, qy, qz, qw]``（与 RPent 的 ``Rotation.from_quat`` 一致，
        坐标/长度单位 = 米；而扁平 ``pose`` 是 edge 原生的 ``xyz + rpy``）。
        """
        frame = self._view.frame()
        payload = self._observation_payload(frame=frame, with_images=False)
        return {
            "ok": True,
            "qpos": payload["qpos"],
            "pose": payload["pose"],
            "arms": payload["arms"],
            "gripper": self._gripper_values(frame),
            "wrapped_state_vector": payload["states"],
            **self._arm_state_blocks(frame),
        }

    def _arm_state_blocks(self, frame: dict) -> dict:
        """每臂状态块（RPent 形态）：``<arm>_arm`` → 关节位置 / 夹爪开合 / quat 位姿。

        ``frame`` 由调用方给定：状态块与同一拍的 qpos / pose 同源（不能各自再取一次缓存）。
        """
        _adapter, arms = self._view.require_adapter_with_arms()
        if not arms:
            return {}
        joint_vector = self._rpent_state_vector(ActionSpace.JOINT, frame=frame)
        pose_vector = self._pose_vector(frame)
        joint_per_arm = self._rpent_dim_per_arm(ActionSpace.JOINT)
        pose_per_arm = self._view.dim_per_arm(ActionSpace.POSE)
        blocks = {}
        for index, arm in enumerate(arms):
            block: dict = {}
            if joint_vector is not None and joint_vector.size >= joint_per_arm * (index + 1):
                segment = joint_vector[index * joint_per_arm : (index + 1) * joint_per_arm]
                block["arm_joint_position"] = segment
                block["gripper_open"] = bool(float(segment[-1]) >= 0.5)
            if pose_vector is not None and pose_per_arm >= 6:
                arm_pose = pose_vector[index * pose_per_arm : (index + 1) * pose_per_arm]
                if arm_pose.size == pose_per_arm:
                    block["tcp_pose"] = np.concatenate([arm_pose[:3], matrix_to_quat(rpy_to_matrix(arm_pose[3:6]))])
            if block:
                blocks[f"{arm}_arm"] = block
        return blocks

    def _get_task_language(self) -> str | None:
        """当前任务语言描述：推理会话的 prompt（无会话 / 未设置 → ``None``）。"""
        return self._view.prompt

    def _observation_payload(
        self,
        *,
        frame: dict | None = None,
        source: str | None = None,
        with_images: bool = True,
        native_images: bool = True,
    ) -> dict:
        """观测帧 → RPC 载荷（键名对齐 RPent 期望 + 保留 edge 原生字段）。

        ``frame`` / ``source`` 由调用方给定时**不再自行取帧**：一帧只取一次，状态 / 夹爪 / 图像
        都取自同一拍（杜绝混拍）；缺省才在这里解析（见 :meth:`_payload_frame`）：
        ``image_source: native`` 时直读 ``adapter.observe()``——**原图 + 同一拍的 qpos / pose**，
        保证「图与状态同时刻」（RPent 会原样落盘 PNG 并内联给模型，降采样会让小物体看不清）；
        读失败 / 配了 ``preview`` 则回落 ``FrameManager`` 缓存（降采样，但永不缺帧）。
        ``native_images=False``（动作块的逐帧观测，面向 VLA、不喂 LLM）直接走缓存，省带宽。
        """
        if frame is None:
            frame, source = self._payload_frame(self._view.frame(), native_images=native_images)
        qpos = as_float_array(frame.get(KEY_QPOS))
        gripper = as_float_array(frame.get(KEY_GRIPPER))
        pose = as_float_array(frame.get(KEY_POSE))
        # RPent 的 ``states`` = wrapped_state_vector = 每臂「关节 6 + 夹爪 1」（与 arm_joint_position
        # 同形）——夹爪在 edge 侧是独立键，这里按 RPent 布局拼**同一拍**的值（不拿缓存凑）。
        states = self._rpent_state_vector(ActionSpace.JOINT, frame=frame)
        if states is None:  # 观测不全（如缺 qpos）→ 退原始 qpos，不编造
            states = qpos
        payload: dict = {
            "states": states,  # RPent：agent 侧 wrapped_state_vector
            "qpos": qpos,
            "gripper": gripper,  # 夹爪（独立观测键；每臂 1 维）
            "pose": pose,
            "arms": self._view.arms,
            "action": as_float_array(frame.get(KEY_ACTION)),
            "image_source": source,
        }
        if with_images:
            frames = self._camera_frames(frame)
            payload["raw_camera_frames"] = frames  # RPent 落盘循环的键
            payload["images"] = sorted(frames)  # 本拍返回的相机名（RPent 记录用）
            payload["image_block_order"] = sorted(frames)
        return payload

    def _payload_frame(self, cached: dict, *, native_images: bool) -> tuple[dict, str]:
        """本拍载荷用的帧 + 来源标签（``native`` / ``preview``）——与状态值同拍的前提。"""
        native = self._observe_native() if native_images else None
        return (native, self._image_source) if native is not None else (cached, "preview")

    def _observe_native(self) -> dict | None:
        """直读 adapter 原图观测（``image_source: preview`` / 无 adapter / 读失败 → ``None``）。"""
        if self._image_source != "native":
            return None
        adapter = self._view.adapter
        if adapter is None:
            return None
        try:
            return adapter.observe()
        except Exception:  # noqa: BLE001 瞬态无帧 / 机器人忙 → 回落缓存，不让 RPC 失败
            return None

    def _camera_frames(self, observation: dict) -> dict[str, np.ndarray]:
        """观测里的相机帧 → ``{相机名: uint8 HWC RGB ndarray}``（JPEG 解码，非空时）。"""
        frames: dict[str, np.ndarray] = {}
        for key, value in observation.items():
            if not (isinstance(key, str) and key.startswith(CAMERA_PREFIX)):
                continue
            array = decode_jpeg(value)
            if array is not None:
                frames[key[len(CAMERA_PREFIX) :]] = array
        return frames

    def _gripper_values(self, frame: dict) -> dict[str, float]:
        """每启用臂的夹爪当前值（``observations/gripper``；无臂概念 → ``{"": value}``）。

        夹爪是**独立动作空间 / 独立观测键**（不拼在关节或位姿值里）。
        """
        arms = self._view.arms
        gripper = as_float_array(frame.get(KEY_GRIPPER))
        if gripper is None or gripper.size == 0:
            return {}
        if not arms:
            return {"": float(gripper[0])}
        if gripper.size < len(arms):
            return {}
        return {arm: float(gripper[index]) for index, arm in enumerate(arms)}

    # ---- 动作空间 ↔ RPent 布局转换（RPent 仍按「每臂 7 维 = 值 + 夹爪」看 edge）--------
    def _pose_vector(self, frame: dict) -> np.ndarray | None:
        """某帧的末端位姿（``observations/pose``，每臂 ``xyz + rpy``）；机器人不提供 → ``None``。"""
        if self._view.dim_per_arm(ActionSpace.POSE) <= 0:
            return None
        return as_float_array(frame.get(KEY_POSE))

    def _rpent_state_vector(
        self, space: ActionSpace, *, frame: dict, values_key: str | None = None
    ) -> np.ndarray | None:
        """观测 → RPent 布局（每臂 ``值 + 夹爪``）——供 ``_joint_base_vector`` /
        ``_pose_base_vector`` / ``_settle_errors`` 用。

        ``joint`` → ``observations/qpos``（关节角）；``pose`` → ``observations/pose``（末端位姿）；
        夹爪恒取自 ``observations/gripper``（独立键）。三者都与动作空间无关，随时可读。
        ``values_key`` 可换掉「值」的来源键（例如取 ``action`` = **关节段目标**，见
        ``_joint_base_vector``）；``frame`` **必传**（不给缺省）：默认取缓存帧最容易拼出**错拍**
        的向量，故强制调用方声明用哪一帧（要当前帧就自己先 ``self._view.frame()`` 一次）。
        """
        if space is ActionSpace.GRIPPER:
            return None
        key = values_key or (KEY_QPOS if space is ActionSpace.JOINT else KEY_POSE)
        values = as_float_array(frame.get(key))
        if values is None:
            return None
        per_arm = self._view.dim_per_arm(space)
        arms = self._view.arms
        count = max(len(arms), 1)
        if per_arm <= 0 or values.size < per_arm * count:
            return None
        gripper = as_float_array(frame.get(KEY_GRIPPER))
        parts: list[np.ndarray] = []
        for index in range(count):
            grip = float(gripper[index]) if gripper is not None and gripper.size > index else 0.0
            parts.append(np.concatenate([values[index * per_arm : (index + 1) * per_arm], [grip]]))
        return np.concatenate(parts).astype(np.float32)

    def _joint_base_vector(self, frame: dict) -> tuple[np.ndarray | None, str]:
        """关节空间「保持当前姿态」的基座 → ``(RPent 向量, base)``，``base`` ∈ ``target`` / ``qpos``。

        优先取 ``action``（**关节段目标**，即底层实际控制量）；机器人没发布该键
        （例如从未下发过指令）才退到 ``observations/qpos``（实测）。

        为什么不直接用实测：底层是 MIT 力矩控制，实测关节恒落后目标一个稳态误差
        （``τ_gravity / kp``，可达 0.05–0.25 rad）。拿实测当基座会把这次的稳态误差**写进新目标**，
        逐步累积，并且抹掉还没走完的运动（上一条目标仍在执行时会被拽回实测值）。
        """
        vector = self._rpent_state_vector(ActionSpace.JOINT, frame=frame, values_key=KEY_ACTION)
        if vector is not None:
            return vector, "target"
        return self._rpent_state_vector(ActionSpace.JOINT, frame=frame), "qpos"

    def _pose_base_vector(self, frame: dict) -> tuple[np.ndarray | None, str]:
        """位姿空间「保持当前位姿」的基座 → ``(RPent 向量, base)``，``base`` ∈ ``pose_target`` / ``pose``。

        优先取 ``observations/pose_target``（**目标**位姿 = ``FK(关节段目标)``）；机器人没发布该键
        （不提供位姿）才回落实测 ``observations/pose``。理由同 ``_joint_base_vector``：底层 MIT 有
        稳态误差，拿实测当基座会把这次的误差写进新目标（动作块逐帧回填时每个 chunk 累积一次），
        还会把未走完的运动拽回实测值。
        """
        vector = self._rpent_state_vector(ActionSpace.POSE, frame=frame, values_key=KEY_POSE_TARGET)
        if vector is not None:
            return vector, "pose_target"
        return self._rpent_state_vector(ActionSpace.POSE, frame=frame), "pose"

    def _split_rpent_action(self, vector: np.ndarray, space: ActionSpace) -> tuple[np.ndarray, np.ndarray]:
        """RPent 布局（每臂 ``值 + 夹爪``）→ （该空间的值, 夹爪值）两份扁平向量，分别下发。

        edge 侧三个空间各自独立（夹爪不并进关节 / 位姿值），故 RPent 的一条动作在这里拆成
        两条命令：值走 ``space``，夹爪走 ``gripper``。
        """
        per_arm = self._view.dim_per_arm(space)
        arms = self._view.arms
        count = max(len(arms), 1)
        values = np.asarray(vector, dtype=np.float32).reshape(-1)
        if per_arm <= 0 or values.size != (per_arm + 1) * count:
            raise RpentError(
                f"action dim {values.size} != {space.value} layout ({(per_arm + 1)} per arm × {count})",
                kind="argument",
            )
        value_parts: list[np.ndarray] = []
        grippers: list[float] = []
        for index in range(count):
            block = values[index * (per_arm + 1) : (index + 1) * (per_arm + 1)]
            value_parts.append(block[:per_arm])
            grippers.append(float(block[per_arm]))
        return np.concatenate(value_parts), np.asarray(grippers, dtype=np.float32)

    # ---- 控制（转发到既有路径）-----------------------------------------------

    def _reset(self) -> dict:
        """``env.reset`` → 命令通道 ``robot/reset``（经 ``CommandService``，含租约与回执）。

        ``dry_run`` 下**不打命令通道**：同样不下发任何动作（否则“不动机器人”的承诺在 reset 上就不成立）。
        """
        if self._dry_run:
            return {"ok": True, "state": "dry_run", "states": self._states(), **self._dry_run_receipt()}
        data = self._invoke_command("robot/reset")
        return {"ok": True, "state": "ready", "states": self._states(), "command": data}

    def _recover_joint_posture(self, reason: str = "", return_to_start: bool = True, settle: Any = None) -> dict:
        """``env.recover_joint_posture`` → 关节回 home、**保持各夹爪当前开合**（与 RPent 工具描述一致）。

        实现仍走既有契约：关节目标 = 每启用臂的 ``HOME["joint"]`` 关节段 + **当前夹爪值**
        （``observations/gripper``，夹爪是独立空间）→ ``rollout(joint)``；adapter 没声明
        关节 home → 退回 ``adapter.reset()``（回执里以 ``fallback`` 标注，不静默换语义）。
        默认阻塞到到位（``settle``）。

        ``return_to_start`` 只**回执回显**（本实现回的都是 ``HOME["joint"]``）：RPent 工具签名
        里带它，语义待定，不假装支持。
        """
        adapter, arms = self._view.require_adapter_with_arms()
        reply = {"ok": True, "reason": str(reason or ""), "return_to_start": bool(return_to_start)}
        target = self._home_target_keeping_grippers(arms, self._view.frame())
        if target is None:  # 无关节 home 声明：只能退回整体复位（含夹爪），回执如实标注
            if not self._dry_run:  # dry_run 下连 reset 也不打
                adapter.reset()
            return {
                **reply,
                "fallback": "adapter.reset()",
                "gripper_preserved": False,
                "states": self._states(),
                **(self._dry_run_receipt() if self._dry_run else {}),
            }
        if self._dry_run:
            return {
                **reply,
                "gripper_preserved": True,
                "target": target,
                "states": self._states(),
                **self._dry_run_receipt(),
            }
        self._push_action(target, ActionSpace.JOINT)
        return {
            **reply,
            "gripper_preserved": True,
            "target": target,
            "states": self._states(),
            **self._settle_action(target, list(range(len(arms) or 1)), ActionSpace.JOINT, settle),
        }

    def _home_target_keeping_grippers(self, arms: list[str], frame: dict):
        """关节回 home + 夹爪保持当前值（RPent 布局：每臂 ``关节 6 + 夹爪 1``）；无 home → ``None``。

        关节 home 取 ``HOME["joint"]``（类常量，**不必读机器人当前关节角**——夹爪已独立成键，
        故即使机器人当下在位姿空间也能回 home）；``frame`` 给当前夹爪值的那一拍。
        """
        home = self._view.home(ActionSpace.JOINT)
        joint_per_arm = self._view.dim_per_arm(ActionSpace.JOINT)
        if not home or joint_per_arm <= 0:
            return None
        gripper = as_float_array(frame.get(KEY_GRIPPER))
        parts: list[np.ndarray] = []
        for index, _arm in enumerate(arms or [""]):
            arm_home = home[index * joint_per_arm : (index + 1) * joint_per_arm]
            if len(arm_home) != joint_per_arm:
                return None
            grip = float(gripper[index]) if gripper is not None and gripper.size > index else 1.0
            parts.append(np.concatenate([np.asarray(arm_home, dtype=np.float32), [grip]]))
        return np.concatenate(parts).astype(np.float32)

    def _move_delta(self, *args, arm: str | None = None, delta_xyz: Any = None, settle: Any = None) -> dict:
        """``env.move_delta``：相对位移 → 用**当前观测位姿**现算绝对目标下发（笛卡尔）。"""
        arm, delta = split_arm_and_vector(args, arm, delta_xyz, "delta_xyz", expected=3)
        return self._pose_delta(arm, delta, what="move_delta", settle=settle)

    def _rotate_delta(self, *args, arm: str | None = None, delta_rpy: Any = None, settle: Any = None) -> dict:
        """``env.rotate_delta``：相对姿态（rpy 增量，弧度）→ 绝对目标下发（笛卡尔）。"""
        arm, delta = split_arm_and_vector(args, arm, delta_rpy, "delta_rpy", expected=3)
        return self._pose_delta(arm, delta, what="rotate_delta", offset=3, settle=settle)

    def _set_gripper(self, *args, arm: str | None = None, open: Any = True, settle: Any = None) -> dict:
        """``env.set_gripper``：夹爪开合 —— 只改夹爪位，其余维**保持当前指令目标**（关节空间）。

        基座取 ``action``（关节段目标）而不是实测 qpos（见 ``_joint_base_vector``）：
        只改夹爪不该把手臂目标重新拉回「实测当前」——那会把 MIT 稳态误差写进目标、并把还没走完
        的运动抹掉。回执 ``base`` 标注实际用的基座（``target`` / ``qpos``）。
        """
        if args:  # RPent 的 dual_franka 形态：set_gripper(arm, open=...)
            arm = args[0] if arm is None else arm
        self._view.require_adapter()  # 未绑定 adapter：与其余写方法一致地报 state 错
        arms = self._view.arms
        space = ActionSpace.JOINT
        indices = self._arm_indices(arm, arms)
        per_arm = self._rpent_dim_per_arm(space)
        vector, base = self._joint_base_vector(self._view.frame())
        if vector is None:
            raise RpentError(
                "current observation has no joint values (action / qpos are read from observations/*)",
                kind="unsupported",
            )
        target = np.array(vector, dtype=np.float32, copy=True)
        wanted = 1.0 if bool(open) else 0.0
        for index in indices:
            target[index * per_arm + per_arm - 1] = wanted
        reply = {"ok": True, "arm": arm, "open": bool(open), "base": base, "action": target}
        if self._dry_run:
            return {**reply, **self._dry_run_receipt()}
        sent = self._push_action(target, space)
        return {**reply, "sent": bool(sent), **self._settle_action(target, indices, space, settle)}

    def _step(self, action: Any = None, action_space: str | None = None) -> list:
        """``env.step``：单帧动作 → 返回 gym 形态 5 元组 ``[obs, reward, terminated, truncated, info]``。

        配了 ``action_layout`` 时先按外部布局转换（见模块 docstring）；``dry_run`` → 只回转换结果。
        """
        if action is None:
            raise RpentError("env.step requires an action", kind="argument")
        space = self._action_space_for(action_space)
        row = self._prepare_actions(action)[0]
        if self._dry_run:
            info = {
                "action_space": space.value,
                "action_layout": self._action_layout or None,
                "dry_run": True,
                "converted": row,
            }
            return [self._observation_payload(), 0.0, False, False, info]
        self._push_action(row, space)
        return [self._observation_payload(), 0.0, False, False, {"action_space": space.value}]

    def _chunk_step(
        self, actions: Any = None, return_all_frames: bool = False, action_space: str | None = None
    ) -> dict:
        """``env.chunk_step``：一整块动作逐帧下发（按控制频率节奏），返回终态观测。

        ``return_all_frames=True`` → ``observation`` 为逐帧观测列表（RPent 的「高密度视频」模式）。
        遥操作（人工接管）中机器人会拒拍：全被拒 → 判失败；部分被拒 → 计数并继续。
        配了 ``action_layout`` 时先按外部布局转换；``dry_run`` → 只回转换结果（不下发、不等待）。
        """
        if actions is None:
            raise RpentError("env.chunk_step requires actions", kind="argument")
        space = self._action_space_for(action_space)
        matrix = self._prepare_actions(actions)
        if matrix.shape[0] == 0:
            raise RpentError("env.chunk_step requires at least one action", kind="argument")
        if self._dry_run:
            final = self._observation_payload(native_images=False)
            return {
                "observation": final,
                "states": final.get("states"),
                "terminated": False,
                "truncated": False,
                "sent": 0,
                "requested": int(matrix.shape[0]),
                "refused": 0,
                "dry_run": True,
                "converted": matrix,
                "action_space": space.value,
                "action_layout": self._action_layout or None,
            }
        period = self._step_period()
        frames: list[dict] = []
        refused = 0
        last = matrix.shape[0] - 1
        for index, row in enumerate(matrix):
            if not self._push_action(row, space, allow_refusal=True):
                refused += 1
            if return_all_frames:
                frames.append(self._observation_payload(native_images=False))
            if period > 0 and index != last:  # 帧间限速；最后一帧后不再多睡一拍
                time.sleep(period)
        if refused and refused == matrix.shape[0]:
            raise RpentError(
                "robot refused every action in the chunk (teleop / human takeover in progress)", kind="state"
            )
        final = self._observation_payload(native_images=False)
        return {
            "observation": frames or final,
            "states": final.get("states"),
            "terminated": False,
            "truncated": False,
            "sent": int(matrix.shape[0] - refused),
            "requested": int(matrix.shape[0]),
            "refused": int(refused),
            "action_space": space.value,
        }

    # ---- 控制内部 -----------------------------------------------------------

    def _pose_delta(
        self, arm: str | None, delta: np.ndarray, *, what: str, offset: int = 0, settle: Any = None
    ) -> dict:
        """位姿增量下发（``pose_delta``）：增量叠加在**机器人的关节段目标**上（本层不算绝对目标）。

        为何不自己算“实测位姿 + 增量”：底层 MIT 只有 P/D、无重力前馈，实测恒落后目标一个稳态
        误差——以实测为基准会把当前误差写进新目标（逐条累积），且上位「读基准 → 算 → 写」之间
        还有窗口（遥操作接管 / CLI 直控 / 别的会话改目标）。增量语义整体归机器人：基准 / 叠加 /
        逆解在机器人侧**同一拍**完成（见 wiki/design/robot_pipeline_cartesian.md「位姿增量」）。

        夹爪**不碰**（本原语只动位姿）：RPent 布局里的夹爪列只是占位，不再拿实测夹爪当绝对目标重
        发（否则会把刚下发、尚未走完的 ``set_gripper`` 目标抹回实测值）。

        到位判定需要**绝对目标**：取自观测 ``observations/pose_target``（= ``FK(关节段目标)``，
        由机器人随位姿一起发布）。下发后先等它从快照跃迁（确认命令已落地），再以跃迁后的目标为
        参考等到位：目标一直未跃迁（超 ``settle.target_wait_s``）→ ``reached: null`` +
        ``not_applied``（不谎报到位）；跃迁后与「快照 + 增量」不符 → ``base_changed``（基准被第三方
        改动，回执如实标注，不默认为自己的）。
        """
        _adapter, arms = self._view.require_adapter_with_arms()
        space = ActionSpace.POSE_DELTA
        if not self._view.supports(space):
            raise RpentError(
                f"adapter does not support {space.value} actions; {what} unavailable "
                "(robot side must declare pose_delta: 增量叠加在关节段目标上)",
                kind="unsupported",
            )
        per_arm = self._rpent_dim_per_arm(space)
        indices = self._arm_indices(arm, arms)
        vector = np.zeros(per_arm * max(len(arms), 1), dtype=np.float32)  # RPent 布局：每臂「值 6 + 夹爪 1」
        for index in indices:
            start = index * per_arm + offset
            vector[start : start + delta.size] += delta
        before = self._rpent_state_vector(ActionSpace.POSE, frame=self._view.frame(), values_key=KEY_POSE_TARGET)
        predicted = None if before is None else self._add_pose_delta(before, vector, per_arm)
        predicted_source = "predicted" if predicted is not None else None
        reply = {"ok": True, "arm": arm, "delta": delta, "action_space": space.value, "states": self._states()}
        if self._dry_run:  # 只回预测的绝对目标，不碰机器人
            return {**reply, "target": predicted, "target_source": predicted_source, **self._dry_run_receipt()}
        self._push_action(vector, space, with_gripper=False)
        reference, flags = self._pose_target_reference(before, predicted, settle)
        if reference is None:  # 命令未落地 / 无目标位姿：不谎报到位
            return {
                **reply,
                "target": predicted,
                "target_source": predicted_source,
                "reached": None,
                "reason": flags.get("reason", "target pose unavailable"),
                **flags,
            }
        return {
            **reply,
            "target": reference,
            "target_source": "pose_target",
            **flags,
            **self._settle_action(reference, indices, ActionSpace.POSE, settle),
        }

    # ---- 到位等待（settle）---------------------------------------------------

    def _pose_target_reference(
        self, before: np.ndarray | None, predicted: np.ndarray | None, override: Any = None
    ) -> tuple[np.ndarray | None, dict]:
        """等 ``observations/pose_target`` 从下发前快照跃迁 → ``(参考目标, 标志)``。

        为什么需要：增量下发走命令队列（异步）、观测按观察频率发布，故「下发返回」不等于「目标已
        改」——不等就会拿**旧目标**当参考，把「实测本就在旧目标附近」误判成到位。

        标志：``no_pose_target``（机器人不发布目标位姿，无法判定）/ ``not_applied``（超出
        ``settle.target_wait_s`` 仍未跃迁）/ ``base_changed``（跃迁后的目标与「快照 + 增量」不符
        ——基准被第三方改动，回执如实标注）。增量为 0 时不等跃迁（本就在目标上）。
        """
        if before is None:
            return None, {"no_pose_target": True, "reason": "robot publishes no observations/pose_target"}
        self._view.require_adapter()  # 未绑定 adapter → state 错（与其余路径一致）
        per_arm = self._rpent_dim_per_arm(ActionSpace.POSE)
        if predicted is not None and not self._pose_values_differ(predicted, before, per_arm):
            return before, {}  # 增量恒为 0：目标未变，直接拿快照当参考
        config = self._settle_config.with_override(override)
        deadline = time.monotonic() + max(config.target_wait_s, config.poll_s)
        while True:
            current = self._rpent_state_vector(ActionSpace.POSE, frame=self._view.frame(), values_key=KEY_POSE_TARGET)
            if current is not None and self._pose_values_differ(current, before, per_arm):
                flags: dict = {}
                if predicted is not None and self._pose_targets_differ_beyond(current, predicted, per_arm, config):
                    flags["base_changed"] = True
                return current, flags
            if time.monotonic() >= deadline:
                return None, {
                    "not_applied": True,
                    "reason": "robot target pose did not move within settle.target_wait_s",
                }
            time.sleep(config.poll_s)

    @staticmethod
    def _pose_values_differ(left: np.ndarray | None, right: np.ndarray | None, per_arm: int) -> bool:
        """两个 RPent 布局向量的**位姿值**（每臂前 6 维）是否不同（夹爪列不参与比较）。"""
        if left is None or right is None or per_arm <= 0:
            return False
        a = np.asarray(left, dtype=np.float64).reshape(-1)
        b = np.asarray(right, dtype=np.float64).reshape(-1)
        for index in range(min(a.size, b.size) // per_arm):
            start = index * per_arm
            if not np.allclose(a[start : start + 6], b[start : start + 6], atol=1e-9, rtol=0.0):
                return True
        return False

    @staticmethod
    def _pose_targets_differ_beyond(left: np.ndarray, right: np.ndarray, per_arm: int, config: SettleConfig) -> bool:
        """两个目标位姿是否差出**到位容差**（位置米 / 姿态弧度分项比）——用于 ``base_changed``。"""
        a = np.asarray(left, dtype=np.float64).reshape(-1)
        b = np.asarray(right, dtype=np.float64).reshape(-1)
        for index in range(min(a.size, b.size) // per_arm):
            start = index * per_arm
            if np.linalg.norm(a[start : start + 3] - b[start : start + 3]) > config.pos_tol:
                return True
            delta_rpy = wrap_angles(a[start + 3 : start + 6] - b[start + 3 : start + 6])
            if np.linalg.norm(delta_rpy) > config.rot_tol:
                return True
        return False

    @staticmethod
    def _add_pose_delta(base: np.ndarray, vector: np.ndarray, per_arm: int) -> np.ndarray:
        """``base + vector``（RPent 布局，逐臂叠加；rpy 相加后 wrap）——仅用于回执里的预测值。"""
        target = np.array(base, dtype=np.float32, copy=True)
        for index in range(min(np.asarray(base).size, np.asarray(vector).size) // per_arm):
            start = index * per_arm
            target[start : start + per_arm] += vector[start : start + per_arm]
            target[start + 3 : start + 6] = wrap_angles(target[start + 3 : start + 6])
        return target

    def _settle_action(self, target: np.ndarray, indices: list[int], space: ActionSpace, override: Any = None) -> dict:
        """下发后等机器人到位 → ``reached`` / ``final_err`` / ``elapsed_s``（RPent 据此判成败）。

        为什么要等：``rollout`` 只负责“设目标”，由机器人侧循环限速靠近（``observe`` 只读缓存）；
        立即返回会让 RPent 紧接着的 ``dump_state`` 看到**尚未动的那一帧** → planner 以为命令无效、
        重试或振荡。容差 / 停滞 / 超时的语义与回执字段见 :mod:`~motrix_edge.server.rpent.settle`。

        ``override`` 是逐次覆盖：``settle=False`` 关闭、``settle={"timeout_s": 60}`` 只改一项
        （长行程 / 慢原语由调用方自己给足时间，不用改部署配置）。
        """
        config = self._settle_config.with_override(override)
        if self._dry_run or not config.enabled:
            return unreached("dry_run" if self._dry_run else "settle disabled")
        if space is ActionSpace.POSE:
            self._view.require_adapter()  # 未绑定 adapter → state 错
            if self._view.dim_per_arm(ActionSpace.POSE) <= 0:
                return unreached("robot reports no end-effector pose (observations/pose)")
        return wait_for_reached(lambda: self._settle_errors(target, indices, space), space=space, config=config)

    def _settle_errors(
        self, target: np.ndarray, indices: list[int], space: ActionSpace
    ) -> tuple[float, float | None, float | None] | None:
        """当前观测与目标的误差 → ``(主误差, 位置误差, 姿态/关节误差)``；无帧 → ``None``。

        - ``pose``：位置误差 = ``‖Δxyz‖``（米）、姿态误差 = ``‖Δrpy‖``（弧度；逐臂取最差），
          主误差 = 两者较大者（只用于回执展示与停滞追踪，**到位判定分项比**，见
          :meth:`~motrix_edge.server.rpent.settle.SettleConfig.within`）；
        - 关节：主误差 = 姿态误差 = 逐维最大 ``|Δq|``（含夹爪那一维）；位置误差为 ``None``。

        当前值走 ``_rpent_state_vector``（观测实测值 + 夹爪）且**每次轮询取当前帧**，所以 MIT 的
        稳态误差会被如实计入，不会把「下发过目标」当成「已到位」。
        """
        self._view.require_adapter()  # 未绑定 adapter → state 错
        per_arm = self._rpent_dim_per_arm(space)
        source = space if space is ActionSpace.POSE else ActionSpace.JOINT
        current_vector = self._rpent_state_vector(source, frame=self._view.frame())
        if current_vector is None or per_arm <= 0:
            return None
        if source is ActionSpace.POSE:
            value_dim = per_arm - 1
            if value_dim < 6:
                return None
            pos_err = 0.0
            rot_err = 0.0
            for index in indices:
                current = current_vector[index * per_arm : index * per_arm + value_dim]
                wanted = target[index * per_arm : index * per_arm + value_dim]
                if current.size != value_dim or wanted.size != value_dim:
                    return None
                pos_err = max(pos_err, float(np.linalg.norm(np.asarray(wanted[:3]) - current[:3])))
                rot_err = max(rot_err, float(np.linalg.norm(wrap_angles(np.asarray(wanted[3:6]) - current[3:6]))))
            return max(pos_err, rot_err), pos_err, rot_err
        worst = 0.0
        for index in indices:
            current = current_vector[index * per_arm : (index + 1) * per_arm]
            wanted = target[index * per_arm : (index + 1) * per_arm]
            if current.size != wanted.size:
                return None
            worst = max(worst, float(np.max(np.abs(wanted - current))))
        return worst, None, worst

    def _rpent_dim_per_arm(self, space: ActionSpace) -> int:
        """RPent 视角的每臂维度：值（joint / pose 每臂 6）+ 夹爪 1 = 7（gripper 空间 → 1）。

        适配器按空间声明**值**维度（``ACTION_DIM_PER_ARM``），夹爪是独立空间，故 RPent 侧的
        「每臂 7 维」由这里拼出来；下发时再拆回两条命令（见 ``_split_rpent_action``）。
        """
        if space is ActionSpace.GRIPPER:
            return max(self._view.dim_per_arm(ActionSpace.GRIPPER), 1)
        return self._view.dim_per_arm(space) + 1

    def _arm_indices(self, arm: str | None, arms: list[str]) -> list[int]:
        """``arm`` → 臂下标列表（``None`` = 全部启用臂；未知臂名 → 拒绝）。"""
        if arm is None or arm == "":
            return list(range(len(arms) or 1))
        name = str(arm).strip().lower()
        if name not in arms:
            raise RpentError(f"unknown arm {arm!r} (enabled: {arms})", kind="argument")
        return [arms.index(name)]

    def _push_action(
        self, action, space: ActionSpace, *, allow_refusal: bool = False, with_gripper: bool = True
    ) -> bool:
        """下发一帧动作：``adapter.rollout``（维度 / 空间不符 → 拒绝；遥操作中 → 返回 False）。

        ``dry_run`` 下**绝不碰 adapter**：各写方法已在前头早返回并给出 ``sent: false`` 回执，
        这里是兜底——真走到这里说明有路径漏了，响亮报错而不是默默把机器人动了。

        ``with_gripper=False``：只发值段（``pose_delta`` 用——增量原语不动夹爪，RPent 布局里的
        夹爪列只是占位；顺手把实测夹爪当绝对目标重发会把未走完的夹爪目标抹回实测值）。
        """
        if self._dry_run:
            raise RpentError(
                "dry_run is enabled: this path would send a real action (refusing to move the robot)",
                kind="state",
            )
        adapter = self._view.require_adapter()
        vector = np.asarray(action, dtype=np.float32).reshape(-1)
        values, grippers = self._split_rpent_action(vector, space)
        try:
            sent = adapter.rollout(values, action_space=space)
            if with_gripper and grippers.size:
                adapter.rollout(grippers, action_space=ActionSpace.GRIPPER)  # 夹爪是独立空间，单独下发
        except ValueError as exc:  # 维度 / 空间不符：不静默补齐
            raise RpentError(str(exc), kind="argument") from exc
        if sent is False and not allow_refusal:
            raise RpentError("robot refused the action (teleop / human takeover in progress)", kind="state")
        return sent is not False

    def _dry_run_receipt(self) -> dict:
        """``dry_run`` 统一回执：明确「没下发、没到位」（自描述里也能看出节点在 dry-run）。"""
        return {"dry_run": True, "sent": False, "reached": None, "reason": "dry_run"}

    def _invoke_command(self, capability: str, params: dict | None = None) -> dict:
        """经 ``CommandService`` 走命令通道（租约 + 回执）：同一条路，不另建下发链路。"""
        if self._commands is None:
            raise RpentError(f"{capability} requires the command service", kind="unsupported")
        try:
            receipt = self._commands.execute(
                command_id=f"rpent-{uuid.uuid4().hex[:12]}",
                lease_id=self._resolve_lease_id(),
                capability=capability,
                params=params or {},
                source=SOURCE_RPENT,  # 来源标记（仅日志 / 排障）
            )
        except CommandError as exc:
            raise RpentError(str(exc), kind="state") from exc
        if receipt.get("status") != "ok":
            raise RpentError(
                str(receipt.get("error") or f"{capability} rejected ({receipt.get('status')})"),
                kind="state",
            )
        return dict(receipt.get("data") or {})

    # ---- 观测 / 频率（读法全在 EdgeNodeView，这里只做 RPent 视角的拼装）----------

    def _states(self):
        """当前状态向量（RPent 缓存为 ``wrapped_state_vector``）。

        关节空间下给 RPent 布局（每臂 ``关节 + 夹爪``，与 ``arm_joint_position`` 同形）；
        机器人在位姿空间（或没发布 qpos）时退回当前观测值（位姿），不编造。
        """
        frame = self._view.frame()
        if self._view.adapter is not None:
            joint_vector = self._rpent_state_vector(ActionSpace.JOINT, frame=frame)
            if joint_vector is not None:
                return joint_vector
        return as_float_array(frame.get(KEY_QPOS))

    def _step_period(self) -> float:
        """动作块逐帧下发的间隔（秒）：配置 ``step_hz`` > 机器人实测频率 > 0（不额外限速）。"""
        try:
            value = float(self._step_hz or self._view.control_hz() or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        return 1.0 / value if value > 0 else 0.0

    def _normalize_space(self, action_space: str | None) -> ActionSpace:
        """动作空间：缺省关节空间；适配器不支持 → 拒绝（不猜语义）。"""
        adapter = self._view.require_adapter()
        try:
            return adapter.normalize_action_space(action_space)
        except ValueError as exc:
            raise RpentError(str(exc), kind="unsupported") from exc

    def _action_space_for(self, action_space: str | None) -> ActionSpace:
        """本次下发的动作空间：配了 ``action_layout`` 时布局说了算（恒为笛卡尔目标）。"""
        if self._action_layout:
            self._view.require_adapter()
            return ActionSpace.POSE
        return self._normalize_space(action_space)

    def _prepare_actions(self, actions: Any) -> np.ndarray:
        """动作块 → 可直接下发的 ``[N, action_dim]`` 矩阵（按需做外部布局转换）。"""
        matrix = as_action_matrix(actions)
        if not self._action_layout:
            return matrix
        return self._convert_layout(matrix)

    def _convert_layout(self, matrix: np.ndarray) -> np.ndarray:
        """外部布局块 → edge 每臂 ``[xyz, rpy, gripper]`` 笛卡尔**绝对目标**（逐帧转换）。

        只做数值映射（按臂切分 / rot6d → rpy / 夹爪域 / 臂名对齐）：不缩放、不猜语义。
        布局未覆盖的 edge 启用臂用**当前目标位姿**（``observations/pose_target``，缺键回落实测
        位姿）回填——保持的是**目标**而不是实测：MIT 有稳态误差，拿实测回填会把误差写进新目标，
        逐 chunk 累积。需 ``pose_dim > 0``。
        """
        self._view.require_adapter()  # 未绑定 adapter → state 错（与其余路径一致）
        layout_arms = resolve_layout(self._action_layout)
        edge_arms = self._view.arms
        if self._view.dim_per_arm(ActionSpace.POSE) <= 0:
            # 布局产出的是**位姿**目标：机器人不声明 pose 空间就没法下发（不拿关节值充数）
            raise RpentError(
                f"action_layout {self._action_layout!r} produces pose targets, "
                "but the adapter does not support pose actions",
                kind="unsupported",
            )
        block = layout_block_dim()
        expected = layout_frame_dim(self._action_layout)
        if matrix.shape[1] != expected:
            raise RpentError(
                f"action_layout {self._action_layout!r} expects {expected} dims per frame, got {matrix.shape[1]}",
                kind="argument",
            )
        covered = [arm for arm in layout_arms if arm in edge_arms]
        if not covered:
            raise RpentError(
                f"action_layout {self._action_layout!r} arms {list(layout_arms)} do not match adapter arms {edge_arms}",
                kind="unsupported",
            )
        per_arm = self._rpent_dim_per_arm(ActionSpace.POSE)
        base = None
        if any(arm not in covered for arm in edge_arms):  # 未覆盖的臂：保持**当前目标**（不是实测）
            base, _ = self._pose_base_vector(self._view.frame())
        frames = []
        for row in matrix:
            target = (
                np.array(base, dtype=np.float32, copy=True)
                if base is not None
                else np.zeros(per_arm * len(edge_arms), dtype=np.float32)
            )
            for index, arm in enumerate(layout_arms):
                if arm not in covered:
                    continue
                offset = index * block
                slot = edge_arms.index(arm) * per_arm
                target[slot : slot + 3] = row[offset : offset + 3]
                target[slot + 3 : slot + 6] = matrix_to_rpy(rot6d_to_matrix(row[offset + 3 : offset + 9]))
                target[slot + 6] = rpent_gripper_to_edge(row[offset + 9])
            frames.append(target)
        return np.asarray(frames, dtype=np.float32)
