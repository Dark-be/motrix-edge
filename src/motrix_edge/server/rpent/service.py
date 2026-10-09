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
    KEY_POSE,
    KEY_POSE_TARGET,
    KEY_QPOS,
    ActionSpace,
)
from motrix_edge.adapter.http_contract import VALUE_LAYOUT_SEPARATOR
from motrix_edge.command import SOURCE_RPENT, CommandError
from motrix_edge.geometry import (
    IDENTITY,
    ROLL_FREE,
    WORLD_ALIAS,
    WORLD_ARM,
    EgoAxes,
    FrameError,
    base_delta_from_ego,
    chart_increment,
    invert_transform,
    pointing_rpy,
    transform_points,
    turned_deg,
)
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
    CARTESIAN_QPOS_DIM_PER_ARM,
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

#: 观测来源：**基座**取目标（写原语的「发到哪」），**到位判定 / 状态回报**取实测（「到了没」）。
SOURCE_TARGET = "target"
SOURCE_MEASURED = "measured"

#: ``source`` → 该来源下 ``joint`` / ``pose`` **值段**的观测键。
#: 夹爪槽恒取 ``joint`` 族那一条（``qpos`` / ``action`` **本身**就是每臂「值 + 夹爪」交错，
#: 而 ``pose`` / ``pose_target`` 只含位姿）——故值段与夹爪槽**同源**，混合基准拼不出来。
_VALUE_KEYS: dict[str, tuple[str, str]] = {
    SOURCE_TARGET: (KEY_ACTION, KEY_POSE_TARGET),
    SOURCE_MEASURED: (KEY_QPOS, KEY_POSE),
}

#: 增量工具的参考系：**末端（工具）系**——``env.move_delta`` / ``env.rotate_delta`` 的
#: ``delta_xyz`` / ``delta_rpy`` 分量语义固定为 **``x = 向前 / y = 向左 / z = 向上``**（由
#: ``server.rpent.ego_axes`` 映射到法兰哪根轴），可用 ``EgoAxes.basis()`` 一步换基到法兰系。
#: **0 位（关节全 0）时这一组轴与基座系重合**（dual piper 实测：法兰 ``+z`` = 基座 ``+x`` 即“前”、
#: 法兰 ``-x`` = 基座 ``+z`` 即“上”），臂一动则跟着末端走。没有 ``space`` 开关。
DELTA_FRAME = "tool"

#: ``look_at`` 的 ``keep``：保持不变的 xyz 取哪里——机器人侧目标位姿（缺省）还是实测位姿。
KEEP_TARGET = "target"
KEEP_MEASURED = "measured"
LOOK_AT_KEEPS = (KEEP_TARGET, KEEP_MEASURED)

#: ``look_at`` 转角超过它就在回执里标 ``large_rotation``（负载 / 线缆风险；不拒绝执行）。
LARGE_ROTATION_DEG = 60.0

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
        depth=None,
        base_cfg: dict | None = None,
        lease_id: str | None = None,
        step_hz: float | None = None,
    ):
        self._view = EdgeNodeView(node)
        self._commands = commands
        self._leases = leases or LeaseManager()
        # 像素 → 3D 坐标（``env.get_object_position``）：复用 ``/v1/depth`` 的深度服务实例
        # （同源：同一份最新观测缓存 + 同一套标定外参）。未注入 → 该方法报 ``unavailable``。
        self._depth = depth
        cfg = dict(((base_cfg or {}).get("server") or {}).get("rpent") or {})
        # 末端（工具）系的轴命名（``ego_axes``；缺省 forward=+z / left=+x / up=+y）：
        # 现场自描述给 agent（它不该写死哪根轴是「前方」），也是 ``look_at`` 的指向轴。
        # 「前后左右上下」到底对应法兰哪根轴是现场事实，故可配 + 随回执 / 自描述回显。
        self._ego_axes = EgoAxes.from_mapping(cfg.get("ego_axes"))
        self._pinned_lease_id = lease_id if lease_id is not None else cfg.get("lease_id")
        self._step_hz = step_hz if step_hz is not None else cfg.get("step_hz")
        self._action_layout = cfg.get("action_layout")
        self._dry_run = bool(cfg.get("dry_run", False))
        # 观测图来源：native = 直读 adapter 原图（RPent 内联给模型，小物体要看得清）；
        # preview = 用 FrameManager 的降采样缓存（320x240，省带宽）。
        self._image_source = str(cfg.get("image_source") or "native").strip().lower()
        settle = dict(cfg.get("settle") or {})
        # 到位容差：**按 MIT 实际稳态误差标定**（底层只有 P/D、**无重力 / 力矩前馈**，容差小于
        # 稳态误差时 `reached` 永远不成立）——edge.yml 只放**部署值**（偏松，先盖住静态误差），
        # 类兜底故意更紧（宁可判不出到位，也不误报）；参数与回执见 ``rpent/settle.py``。
        self._settle_config = SettleConfig.from_mapping(settle)
        self._handlers = {
            "healthz": self._healthz,
            "session.register": self._session_noop,
            "session.close": self._session_noop,
            "env.get_env_meta": self._get_env_meta,
            "env.get_camera_meta": self._get_camera_meta,
            "env.get_observation": self._get_observation,
            "env.get_robot_state": self._get_robot_state,
            "env.get_object_position": self._get_object_position,
            "env.get_task_language": self._get_task_language,
            "env.reset": self._reset,
            "env.move_delta": self._move_delta,
            "env.rotate_delta": self._rotate_delta,
            "env.move_to": self._move_to,
            "env.look_at": self._look_at,
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
        pose_dim = view.value_dim(ActionSpace.POSE)
        qpos_stride = self._qpos_stride(ActionSpace.JOINT)
        return {
            "ok": True,
            "edge": {"adapter_name": view.adapter_name, "adapter_type": view.adapter_type, "node_state": view.state},
            "explicit_reset_only": True,
            # RPent 侧一帧动作 = 一条 **qpos**（每臂「值 + 夹爪」：joint / pose 各 6 + 夹爪 1）
            "action_dim": qpos_stride * len(arms),
            "action_space": ActionSpace.JOINT.value,
            "action_spaces": [space_value(space) for space in view.action_spaces],
            "action_dim_per_arm": qpos_stride,
            "cartesian_dim_per_arm": CARTESIAN_QPOS_DIM_PER_ARM,
            "pose_dim_per_arm": pose_dim,
            # 位姿约定：读（``observations/pose``）与写（``pose`` 动作）同系，均为 `xyz + rpy`
            "pose_convention": "xyz_rpy" if pose_dim else None,
            # 读（位姿值）与写（pose 动作）必须同系；机器人侧用哪个系由它自己声明
            "pose_frame": view.pose_frame,
            "gripper_range": [0.0, 1.0],  # 0 = 闭合，1 = 张开（独立 gripper 空间，每臂 1 维）
            "arms": arms,
            "all_arms": view.all_arms,
            # ``env.get_observation`` 返回的键（相机按名展开）；``qpos`` = 状态向量、``action`` = 目标向量
            "observation_keys": ["states", "qpos", "gripper", "pose", "action", "raw_camera_frames", "images"],
            "call_endpoint": "/call",
            # 末端（工具）系的轴命名（``space`` 已删：增量**只**有末端系语义）/ ``look_at`` 的指向轴
            # 都是**装配事实**，由本字段自描述（RPent 侧不要写死），现场可改 ``server.rpent.ego_axes``。
            "ego_axes": self._ego_axes.as_dict(),
            "delta_frame": DELTA_FRAME,
            "delta_axes": ["forward", "left", "up"],
            "move_to": {"input_frame": "world"},
            "look_at": {
                "pointing_axis": self._ego_axes.forward,
                "roll": ROLL_FREE,
                "keeps": list(LOOK_AT_KEEPS),
                "large_rotation_deg": LARGE_ROTATION_DEG,
                "input_frame": "world",
                # ``world`` 点会被换算到**该臂基座系**再算方向（见 ``_point_in_arm_base``）：
                # 非 ``left`` 臂要有外参，否则报 ``uncalibrated``。
                "world_arm": WORLD_ARM,
            },
            "object_position": {"input": "normalized u / v", "source": "/v1/depth"},
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
        qpos = self._qpos(ActionSpace.JOINT, frame=frame)
        pose_per_arm = self._view.value_dim(ActionSpace.POSE)
        pose = as_float_array(frame.get(KEY_POSE)) if pose_per_arm > 0 else None
        stride = self._qpos_stride(ActionSpace.JOINT)
        blocks = {}
        for index, arm in enumerate(arms):
            block: dict = {}
            if qpos is not None and qpos.size >= stride * (index + 1):
                segment = qpos[index * stride : (index + 1) * stride]
                block["arm_joint_position"] = segment
                block["gripper_open"] = bool(float(segment[-1]) >= 0.5)
            if pose is not None and pose_per_arm >= 6 and pose.size >= pose_per_arm * (index + 1):
                arm_pose = pose[index * pose_per_arm : (index + 1) * pose_per_arm]
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
        gripper = self._gripper_slots(frame, max(len(self._view.arms), 1))
        pose = as_float_array(frame.get(KEY_POSE))
        # RPent 的 ``states`` = ``wrapped_state_vector`` = 实测 qpos（每臂「值 + 夹爪」交错）
        states = self._state_vector(frame)
        payload: dict = {
            "states": states,  # RPent：agent 侧 wrapped_state_vector
            "qpos": qpos,
            "gripper": gripper,  # 夹爪（状态向量每臂块的末位，扁平每臂 1 维——展示 / 工具用）
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
        except Exception as exc:  # noqa: BLE001 瞬态无帧 / 机器人忙 → 回落缓存，不让 RPC 失败
            debug_print("rpent", f"native observation failed ({exc}); using cached frame", "WARNING")
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

    def _gripper_slots(self, frame: dict, count: int, *, source: str = SOURCE_MEASURED) -> np.ndarray | None:
        """``source`` 那条 **qpos** 里每臂的**夹爪槽**（每臂末位）→ 扁平 ``count`` 维。

        夹爪不单独成观测键：它与值段同属一条 qpos（每臂「值 + 夹爪」交错），故唯一读法就是取每臂
        末位；``source`` 与 :meth:`_qpos` 共用同一口径（``target`` = 基座，``measured`` = 到位判定 /
        状态回报），混合来源在 API 上就拼不出来。
        """
        values = as_float_array(frame.get(_VALUE_KEYS[source][0]))
        stride = self._qpos_stride(ActionSpace.JOINT)
        if values is None or stride <= 0 or values.size < stride * count:
            return None
        return values[: stride * count].reshape(count, stride)[:, -1].copy()

    def _gripper_values(self, frame: dict) -> dict[str, float]:
        """每启用臂的夹爪**实测**值（状态向量的夹爪槽；无臂概念 → ``{"": value}``）。

        供 ``env.get_robot_state`` 展示——「到了没」看实测，与基座（取 ``action`` 的目标夹爪）分开。
        """
        arms = self._view.arms
        gripper = self._gripper_slots(frame, max(len(arms), 1))
        if gripper is None or gripper.size == 0:
            return {}
        if not arms:
            return {"": float(gripper[0])}
        if gripper.size < len(arms):
            return {}
        return {arm: float(gripper[index]) for index, arm in enumerate(arms)}

    # ---- qpos（每臂「值 + 夹爪」拼装）读写 --------------------------------------
    def _qpos(self, space: ActionSpace, *, frame: dict, source: str = SOURCE_MEASURED) -> np.ndarray | None:
        """读一条 **qpos**（每臂「值 + 夹爪」交错，扁平 ``臂数 × stride``）；读不到 → ``None``。

        ``space`` 决定**值段**语义（joint / pose —— 真正的控制内容），``source`` 决定读目标还是实测
        （观测键的唯一定义点见 ``_VALUE_KEYS``）：joint 的值段**就是**那条 qpos（``action`` / ``qpos``
        自带夹爪槽）；pose 的值段来自 ``pose_target`` / ``pose``（每臂 6 维、不含夹爪），夹爪槽从
        **同来源**的 joint 向量每臂末位补上——两侧同源，不会拼出「目标值 + 实测夹爪」这种混合基准。

        ``frame`` **必传**（不给缺省）：默认取缓存帧最容易拼出**错拍**的向量，故强制调用方声明用哪一帧
        （要当前帧就自己先 ``self._view.frame()`` 一次）。
        """
        if space is ActionSpace.GRIPPER:
            return None
        stride = self._qpos_stride(space)
        count = max(len(self._view.arms), 1)
        joint_key, pose_key = _VALUE_KEYS[source]
        values = as_float_array(frame.get(joint_key if space is ActionSpace.JOINT else pose_key))
        if stride <= 0 or values is None:
            return None
        if space is ActionSpace.JOINT:  # qpos 自带夹爪槽，原样裁到启用臂
            return values[: stride * count].astype(np.float32) if values.size >= stride * count else None
        value_dim = stride - 1
        gripper = self._gripper_slots(frame, count, source=source)
        if gripper is None or values.size < value_dim * count:
            return None
        blocks = [
            np.concatenate([values[index * value_dim : (index + 1) * value_dim], [gripper[index]]])
            for index in range(count)
        ]
        return np.concatenate(blocks).astype(np.float32)

    def _qpos_base(self, space: ActionSpace, frame: dict) -> tuple[np.ndarray | None, str | None]:
        """写原语的基座 → ``(qpos, source)``：**优先目标**，机器人没发布目标才**整体**退实测。

        为什么不用实测当基座：底层是 MIT 力矩控制，实测恒落后目标一个稳态误差（``τ_gravity / kp``，
        可达 0.05–0.25 rad）——拿实测当基座会把这次的误差写进新目标（逐个原语累积），并把还没走完的
        运动拽回实测值。退避时值段与夹爪槽一起退（``_qpos`` 保证同源），不拼混合基准。
        """
        for source in (SOURCE_TARGET, SOURCE_MEASURED):
            vector = self._qpos(space, frame=frame, source=source)
            if vector is not None:
                return vector, source
        return None, None

    # ---- 控制（转发到既有路径）-----------------------------------------------

    def _reset(self) -> dict:
        """``env.reset`` → 命令通道 ``robot/reset``（经 ``CommandService``，含租约与回执）。

        ``dry_run`` 下**不打命令通道**：同样不下发任何动作（否则“不动机器人”的承诺在 reset 上就不成立）。
        """
        states = self._state_vector(self._view.frame())
        if self._dry_run:
            return {"ok": True, "state": "dry_run", "states": states, **self._dry_run_receipt()}
        data = self._invoke_command("robot/reset")
        return {"ok": True, "state": "ready", "states": states, "command": data}

    def _recover_joint_posture(self, reason: str = "", return_to_start: bool = True, settle: Any = None) -> dict:
        """``env.recover_joint_posture`` → 关节回 home、**保持各夹爪开合**（与 RPent 工具描述一致）。

        关节目标 = 每启用臂的 ``HOME["joint"]`` 关节段 + **夹爪基座**（目标优先，见 ``_qpos_base``）
        → ``rollout(joint)``；adapter 没声明关节 home → 退回 ``adapter.reset()``（回执以 ``fallback``
        标注，不静默换语义）。默认阻塞到到位（``settle``）。

        夹爪取**目标**而不是实测：与 ``env.set_gripper`` 同规则——拿实测当基座会把 MIT 稳态误差写进
        新目标，并在上一条夹爪目标还没走完时把它拽回实测值（“保持已夹持物”就保不住了）。
        回执 ``source`` 标注夹爪基座的实际来源（``target`` / ``measured``）。

        ``return_to_start`` 只**回执回显**（本实现回的都是 ``HOME["joint"]``）：RPent 工具签名
        里带它，语义待定，不假装支持。
        """
        adapter, arms = self._view.require_adapter_with_arms()
        frame = self._view.frame()
        reply = {"ok": True, "reason": str(reason or ""), "return_to_start": bool(return_to_start)}
        target, source = self._recover_target(arms, frame)
        if target is None:  # 无关节 home 声明：只能退回整体复位（含夹爪），回执如实标注
            if not self._dry_run:  # dry_run 下连 reset 也不打
                adapter.reset()
            return {
                **reply,
                "fallback": "adapter.reset()",
                "gripper_preserved": False,
                "source": source,
                "states": self._state_vector(frame),
                **(self._dry_run_receipt() if self._dry_run else {}),
            }
        reply = {
            **reply,
            "gripper_preserved": True,
            "source": source,
            "target": target,
            "states": self._state_vector(frame),
        }
        return {**reply, **self._send_qpos(target, ActionSpace.JOINT, list(range(len(arms) or 1)), settle)}

    def _recover_target(self, arms: list[str], frame: dict) -> tuple[np.ndarray | None, str | None]:
        """关节回 home + 保持夹爪开合 → ``(qpos 目标, 夹爪来源)``；无关节 home 声明 → ``(None, None)``。

        关节段取 ``HOME["joint"]`` 常量（**不必读当前关节角**，故机器人当下发的是位姿也能回 home）；
        夹爪槽取**基座**（``_qpos_base``：目标优先、缺目标整体退实测）——与 ``set_gripper`` 同一口径，
        所以「保持已夹持物」保得住。夹爪读不到 → 按全开处理（不静默拢住）。
        """
        home = self._view.home(ActionSpace.JOINT)
        value_dim = self._view.value_dim(ActionSpace.JOINT)
        stride = self._qpos_stride(ActionSpace.JOINT)
        if not home or value_dim <= 0:
            return None, None
        base, source = self._qpos_base(ActionSpace.JOINT, frame)
        parts: list[np.ndarray] = []
        for index, _arm in enumerate(arms or [""]):
            arm_home = home[index * value_dim : (index + 1) * value_dim]
            if len(arm_home) != value_dim:
                return None, None
            slot = index * stride + value_dim  # 夹爪槽 = 每臂末位
            grip = float(base[slot]) if base is not None and base.size > slot else 1.0
            parts.append(np.concatenate([np.asarray(arm_home, dtype=np.float32), [grip]]))
        return np.concatenate(parts).astype(np.float32), source

    def _move_delta(
        self,
        *args,
        arm: str | None = None,
        delta_xyz: Any = None,
        settle: Any = None,
    ) -> dict:
        """``env.move_delta``：**沿末端语义轴**的相对位移 → 下发 ``pose_delta`` 增量。

        ``delta_xyz``（米）的分量固定为 **``x = 向前 / y = 向左 / z = 向上``**（0 位时与基座系一致：
        实测法兰 ``+z`` = 基座 ``+x``（前）、法兰 ``-x`` = 基座 ``+z``（上）），再由 ``ego_axes``
        映射到法兰哪根轴——**不是**“直接拿法兰 xyz”（那在 0 位会变成往下走）。上位用当前**目标位姿**
        的姿态换算到该臂基座系（``d_base = R_cur · d_tool``），叠加仍归机器人侧（见 ``_delta_tool_frame``）。
        """
        arm, delta = split_arm_and_vector(args, arm, delta_xyz, "delta_xyz", expected=3)
        return self._delta_tool_frame(arm, delta, what="move_delta", offset=0, settle=settle)

    def _rotate_delta(
        self,
        *args,
        arm: str | None = None,
        delta_rpy: Any = None,
        settle: Any = None,
    ) -> dict:
        """``env.rotate_delta``：**绕末端语义轴**的相对姿态（``delta_rpy`` 弧度）。

        “左右转 / 上下转 / 自转” = 绕 **``z = 向上`` / ``y = 向左`` / ``x = 向前``** 三轴（0 位时与基座系
        一致），由 ``ego_axes`` 映射到法兰轴后换基：``ΔR_flange = M · ΔR_semantic · Mᵀ``，再算
        **chart 增量**下发。不下发 ``matrix_to_rpy(R · ΔR · Rᵀ)``：机器人把 rpy **逐分量相加**，
        两者在目标 ``pitch`` 不为 0 时差得很多（``pitch=45°`` 绕轴转 20° 偏 10.7°）——见 ``chart_increment``。
        """
        arm, delta = split_arm_and_vector(args, arm, delta_rpy, "delta_rpy", expected=3)
        return self._delta_tool_frame(arm, delta, what="rotate_delta", offset=3, settle=settle)

    def _delta_tool_frame(self, arm: str | None, delta: np.ndarray, *, what: str, offset: int, settle: Any) -> dict:
        """语义系增量 → 基座系增量后下发（唯一的增量路径，不再有 ``space`` 开关）。

        为什么换算必须在上位：机器人侧的 ``pose_delta`` 是**基座系**的 chart 相加
        （基准 = ``FK(关节段目标)``），它不知道“沿末端哪根轴”。换算用**快照**
        （``observations/pose_target`` 的姿态），窗口内被第三方（遥操作 / CLI / 别的会话）改了目标 →
        既有的 ``base_changed`` 检查会标出来；快照本身就不可用 → 报 ``state``，不猜。

        分量语义（``x/y/z`` = 前/左/上）先经 ``ego_axes.basis()`` 换到**法兰系**（0 位时这组轴与基座
        系重合），再做“法兰系 → 基座系”：平移旋转矢量，旋转换基后取 chart 增量。
        """
        _adapter, arms = self._view.require_adapter_with_arms()
        if not self._view.supports(ActionSpace.POSE_DELTA):
            raise RpentError(
                f"adapter does not support {ActionSpace.POSE_DELTA.value} actions; {what} unavailable "
                "(robot side must declare pose_delta: 增量叠加在关节段目标上)",
                kind="unsupported",
            )
        indices = self._arm_indices(arm, arms)
        stride = self._qpos_stride(ActionSpace.POSE)
        snapshot = self._qpos(ActionSpace.POSE, frame=self._view.frame(), source=SOURCE_TARGET)
        vector = np.zeros(stride * max(len(arms), 1), dtype=np.float32)
        converted: list[np.ndarray] = []
        semantic = np.asarray(delta, dtype=np.float64).reshape(3)[:3]  # (forward, left, up)
        basis = self._ego_axes.basis()  # 语义系 → 法兰系
        for index in indices:
            start = index * stride
            rpy = None if snapshot is None else np.asarray(snapshot, dtype=np.float64)[start + 3 : start + 6]
            if rpy is None or not np.all(np.isfinite(rpy)):
                raise RpentError(
                    f"{what}: 末端系增量需要当前**目标位姿**（observations/pose_target）的姿态做换算，"
                    "但机器人没有发布它——先确认位姿观测",
                    kind="state",
                )
            step = (
                base_delta_from_ego(rpy, basis @ semantic)
                if offset == 0
                else chart_increment(rpy, basis @ rpy_to_matrix(semantic) @ basis.T)
            )
            converted.append(np.asarray(step, dtype=np.float64))
            vector[start + offset : start + offset + 3] += np.asarray(step, dtype=np.float32)
        return self._pose_delta(
            arm,
            delta,
            what=what,
            settle=settle,
            vector=vector,
            extra={
                "delta_frame": DELTA_FRAME,
                "delta_axes": ["forward", "left", "up"],  # delta_xyz / delta_rpy 的分量语义
                "converted_delta_base": converted,
                "ego_axes": self._ego_axes.as_dict(),
                "base_source": SOURCE_TARGET,
            },
        )

    def _move_to(
        self,
        *args,
        target: Any = None,
        arm: str | None = None,
        rpy: Any = None,
        settle: Any = None,
    ) -> dict:
        """``env.move_to``：把末端**移到 ``world`` 帧的给定点**（绝对目标，机器人侧 IK 解算）。

        ``target`` = ``world``（= 左臂基座，米）下的 xyz；``rpy`` 可选（同样是 ``world`` 帧的末端
        姿态，不给 = 保持当前目标姿态）。这是**唯一的绝对位移工具**，也是它跟增量工具的分工：
        增量看末端自己（``move_delta`` / ``rotate_delta``），绝对看 ``world``（``move_to`` /
        ``look_at`` / ``get_object_position`` 三者同一套坐标系，agent 不用换算）。

        内部按 ``T_world_base(arm)`` 换算到该臂基座系（与 ``look_at`` 同一套，见
        ``_point_in_arm_base``）；非左臂未标定 → ``uncalibrated``，不拿错坐标系去动。
        """
        if args:  # 位置参数：(target) 或 (arm, target)
            values = list(args)
            if arm is None and values and isinstance(values[0], str):
                arm = str(values.pop(0))
            if target is None and values:
                target = values.pop(0)
        point = np.asarray(target, dtype=np.float64).reshape(-1)
        if point.size < 3 or not np.all(np.isfinite(point[:3])):
            raise RpentError(
                f"move_to: target must be 3 finite numbers (world frame, meters), got {target!r}",
                kind="argument",
            )
        point = point[:3]
        rpy_world = None
        if rpy is not None:
            rpy_world = np.asarray(rpy, dtype=np.float64).reshape(-1)
            if rpy_world.size < 3 or not np.all(np.isfinite(rpy_world[:3])):
                raise RpentError(f"move_to: rpy must be 3 finite numbers (world frame), got {rpy!r}", kind="argument")
            rpy_world = rpy_world[:3]
        _adapter, arms = self._view.require_adapter_with_arms()
        if self._view.value_dim(ActionSpace.POSE) <= 0:
            raise RpentError(
                "move_to 需要机器人提供末端位姿（observations/pose）：本机型没声明",
                kind="unsupported",
            )
        frame = self._view.frame()
        pose = self._qpos(ActionSpace.POSE, frame=frame, source=KEEP_TARGET)
        base_source = "pose_target"
        if pose is None:  # 目标位姿不可用 → 整条退实测（姿态基准与回执标注一致，不混源）
            pose, base_source = self._qpos(ActionSpace.POSE, frame=frame, source=SOURCE_MEASURED), "pose"
        if pose is None:
            raise RpentError(
                "move_to 需要当前末端位姿（pose_target / pose）——机器人没有发布它",
                kind="state",
            )
        indices = self._arm_indices(arm, arms)
        stride = self._qpos_stride(ActionSpace.POSE)
        command = np.asarray(pose, dtype=np.float32).copy()
        targets: list[list[float]] = []
        for index in indices:
            start = index * stride
            name = str(arms[index]) if index < len(arms) else ""
            point_base = self._point_in_arm_base(_adapter, name, point)
            command[start : start + 3] = point_base.astype(np.float32)
            if rpy_world is not None:
                # ``world`` 下的姿态 → 该臂基座系：``R_base = R_base_world · R_world``。
                T_world_base = self._world_from_base(_adapter, name)
                command[start + 3 : start + 6] = matrix_to_rpy(
                    T_world_base[:3, :3].T @ rpy_to_matrix(rpy_world)
                ).astype(np.float32)
            targets.append([round(float(value), 6) for value in point_base])
        reply = {
            "ok": True,
            "arm": arm,
            "action_space": ActionSpace.POSE.value,
            "input_frame": "world",
            "base_source": base_source,
            "target_base": targets,  # 换算到各臂基座系后的点（与 indices 同序，便于现场核对）
            "target": command,
            "states": self._state_vector(frame),
        }
        if self._dry_run:
            return {**reply, **self._dry_run_receipt()}
        sent = self._push_qpos(command, ActionSpace.POSE, with_gripper=False)
        return {**reply, "sent": sent, **self._settle_action(command, indices, ActionSpace.POSE, settle)}

    def _get_object_position(
        self,
        *args,
        camera: str | None = None,
        x: Any = None,
        y: Any = None,
    ) -> dict:
        """``env.get_object_position``：**归一化**像素 → 该点的 3D 坐标（米，``world`` 帧）。

        与 ``GET /v1/depth`` **同一份实现**（同源观测缓存 + 同一套内参 / 外参），只把回执整成 agent
        好读的形状，并把失败原因**分开**：

        - ``no_depth``：该像素没有有效深度（原始值 0，不是「距离 0」）；
        - ``uncalibrated``：没有外参（未标定 / 产物里没有该相机）——连相机系坐标都给不出；
        - ``no_pose``：腕相机的世界坐标需要**同一拍**的臂位姿，这一拍没有——``xyz_camera`` 照常给。

        只收**归一化**坐标（``x`` / ``y`` ∈ [0, 1]，0.5 = 画面中心）：预览是 320×240 降采样图，与
        源分辨率（如 640×480）不是同一网格，让 agent 自己换算像素反而容易错——统一归一化，回执
        回显 ``u_px`` / ``v_px`` 供核对。

        不可用时回执仍是 ``ok=true``（调用本身成功，只是这个点没答案）：原因在 ``kind`` / ``reason``，
        不逼 agent 走「重试」分支。
        """
        if args:  # 位置参数：(camera, x, y) 或 (x, y)
            values = list(args)
            if camera is None and values and isinstance(values[0], str):
                camera = str(values.pop(0))
            if x is None and values:
                x = values.pop(0)
            if y is None and values:
                y = values.pop(0)
        if self._depth is None:
            raise RpentError(
                "env.get_object_position 需要深度服务（未注入）——用 GET /v1/depth 直查或启用该面",
                kind="unavailable",
            )
        name = str(camera or "").strip()
        if not name:
            raise RpentError("get_object_position: camera is required", kind="argument")
        if x is None or y is None:
            raise RpentError("get_object_position: x / y（归一化）are required", kind="argument")
        try:
            u, v = float(x), float(y)
        except (TypeError, ValueError) as exc:
            raise RpentError(f"get_object_position: x / y must be numbers, got {x!r} / {y!r}", kind="argument") from exc
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            raise RpentError(
                f"get_object_position: x / y must be **normalized** in [0, 1] (0.5 = 画面中心), got {u} / {v}",
                kind="argument",
            )
        body = self._depth.depth(camera=name, u=u, v=v, lease_id=self._resolve_lease_id())
        available = body.get("xyz_world") is not None
        kind: str | None = None
        reason: str | None = None
        if not body.get("valid") or body.get("depth_m") is None:
            kind, reason = "no_depth", "该像素没有有效深度（深度原始值 0）——换一个点或换一台相机"
        elif body.get("xyz_camera") is None:
            kind, reason = "uncalibrated", "没有可用外参（未标定 / 产物里没有这台相机）"
        elif not available:
            kind, reason = (
                "no_pose",
                "腕相机的世界坐标要用**同一拍**的臂位姿合成，这一拍没有——先让臂停稳再查",
            )
        return {
            "ok": True,
            "available": available,
            "kind": kind,
            "reason": reason,
            "camera": body.get("camera"),
            "x": u,
            "y": v,
            "u_px": body.get("u_px"),
            "v_px": body.get("v_px"),
            "depth_m": body.get("depth_m"),
            "valid": body.get("valid"),
            "xyz_camera": body.get("xyz_camera"),
            "xyz_world": body.get("xyz_world"),
            "frame": body.get("frame"),
            "world": body.get("world"),
            "source": "/v1/depth",
        }

    def _look_at(
        self,
        *args,
        target: Any = None,
        arm: str | None = None,
        keep: str = KEEP_TARGET,
        settle: Any = None,
    ) -> dict:
        """``env.look_at``：把末端工具轴指向 ``target``（``world`` 帧 / 米），**位置不变**。

        工具轴 = ``ego_axes.forward``（缺省法兰 ``+z`` = 夹爪 / 探针伸出方向）；姿态取**无 roll**
        的规范解（``roll ≡ 0``）：``z`` 轴指向给 ``pitch = acos(±u_z)``、``yaw = atan2(±u_y, ±u_x)``。
        指向轴若被配成 ``y`` 轴，``roll = 0`` 时它恒在水平面内 → 只能指向与末端同高的点，此处
        **直接报错**（``unsupported``），不悄悄给一个歪头解。

        ``target`` 是 ``world`` 点，而末端 xyz 在各臂**自己的基座系**（``observations/pose`` = 该臂
        ``FK(q)``）——故逐臂先按 ``T_world_base`` 换算到基座系再算方向（左臂恒等；右臂差一个基座
        间距，直接用会指错数十度）。没有标定产物时：左臂照做（``world`` 的定义就是它），**其余臂报
        ``uncalibrated``**——不拿一个错方向去转姿态。回执 ``target_base`` 给出换算后的点供核对。

        ``keep`` 决定「不变」的 xyz 取哪里：``target``（缺省）= 机器人侧**目标位姿**（不把 MIT 稳态
        误差写进新目标，与写原语同一口径）；``measured`` = 实测位姿（让姿态绕「现在真实所在的位置」
        转）。目标位姿不可用时**整条**退实测并在回执 ``base_source`` 标注，不混源。

        下发的是**绝对** ``pose`` 目标（机器人侧 IK 解算），随后按 ``settle`` 判到位：位置不变、只改
        姿态**也可能**解不出来（腕部奇异 / 超关节限位）→ 机器人侧拒绝 → ``ok=false`` /
        ``reached=false``；此时正确做法是先 ``move_delta`` 挪一点再 ``look_at``。
        """
        if args:  # 位置参数：(target) 或 (arm, target)
            values = list(args)
            if arm is None and values and isinstance(values[0], str):
                arm = str(values.pop(0))
            if target is None and values:
                target = values.pop(0)
        point = np.asarray(target, dtype=np.float64).reshape(-1)
        if point.size < 3 or not np.all(np.isfinite(point[:3])):
            raise RpentError(
                f"look_at: target must be 3 finite numbers (world frame, meters), got {target!r}",
                kind="argument",
            )
        point = point[:3]
        source = str(keep or KEEP_TARGET).strip().lower()
        if source not in LOOK_AT_KEEPS:
            raise RpentError(f"look_at: keep must be one of {list(LOOK_AT_KEEPS)}, got {keep!r}", kind="argument")
        _adapter, arms = self._view.require_adapter_with_arms()
        if self._view.value_dim(ActionSpace.POSE) <= 0:
            raise RpentError(
                "look_at 需要机器人提供末端位姿（observations/pose）：本机型没声明",
                kind="unsupported",
            )
        frame = self._view.frame()
        # 回执里的 ``base_source`` 说明「不变的 xyz 取自哪里」——keep 已指定实测时不能被下面的
        # 「目标不可用 → 退实测」分支盖掉，故这里就按 keep 定死。
        base_source = "pose_target" if source == KEEP_TARGET else "pose"
        pose = self._qpos(ActionSpace.POSE, frame=frame, source=source)
        if pose is None and source == KEEP_TARGET:  # 目标位姿不可用 → 整条退实测并标注
            pose, base_source = self._qpos(ActionSpace.POSE, frame=frame, source=SOURCE_MEASURED), "pose"
        if pose is None:
            raise RpentError(
                "look_at 需要当前末端位姿（pose_target / pose）——机器人没有发布它",
                kind="state",
            )
        indices = self._arm_indices(arm, arms)
        stride = self._qpos_stride(ActionSpace.POSE)
        snapshot = np.asarray(pose, dtype=np.float64)
        command = np.asarray(pose, dtype=np.float32).copy()
        turned: list[float] = []
        directions: list[list[float]] = []
        for index in indices:
            start = index * stride
            # ``target`` 是 **world** 点，而 ``snapshot[start:start+3]`` 是**该臂基座系**的 xyz——
            # 两者不同系，必须先换算再相减（左臂恒等；右臂差一个基座间距，直接用会指错 40°+）。
            name = str(arms[index]) if index < len(arms) else ""
            point_base = self._point_in_arm_base(_adapter, name, point)
            try:
                rpy_new = pointing_rpy(self._ego_axes.forward, point_base - snapshot[start : start + 3])
            except ValueError as exc:
                raise RpentError(f"look_at: {exc}", kind="unsupported") from exc
            turned.append(turned_deg(snapshot[start + 3 : start + 6], rpy_new))
            directions.append([round(float(value), 6) for value in point_base])
            command[start + 3 : start + 6] = rpy_new.astype(np.float32)
        reply = {
            "ok": True,
            "arm": arm,
            "action_space": ActionSpace.POSE.value,
            "keep": source,
            "base_source": base_source,
            "input_frame": "world",
            "target_base": directions,  # 换算到各臂基座系后的点（与 turned_deg 同序，便于现场核对）
            "pointing_axis": self._ego_axes.forward,
            "roll": ROLL_FREE,
            "turned_deg": turned,
            "large_rotation": bool(turned) and max(turned) > LARGE_ROTATION_DEG,
            "target": command,
            "states": self._state_vector(frame),
        }
        if self._dry_run:
            return {**reply, **self._dry_run_receipt()}
        sent = self._push_qpos(command, ActionSpace.POSE, with_gripper=False)
        return {**reply, "sent": sent, **self._settle_action(command, indices, ActionSpace.POSE, settle)}

    def _world_from_base(self, adapter, arm: str) -> np.ndarray:
        """``T_world_base(arm)``：``world`` 点 / 姿态 ↔ 该臂基座系的唯一换算入口。

        ``world`` 的定义就是左臂基座（``WORLD_ARM`` / ``WORLD_ALIAS``）——该臂**恒等**、不需要产物；
        其余臂靠产物里的 ``T_world_base``。缺产物 / 该臂没声明锚定 → ``uncalibrated``：宁可拒绝，
        也不拿一个差一个基座间距（这台约 642 mm）的坐标系去动。
        """
        frames = getattr(adapter, "frame_set", None)
        if callable(frames):  # 真实适配器上是**方法**（``HttpShmAdapter.frame_set()``，惰性查询 + 缓存）；
            frames = frames()  # 假件 / 未来实现可能直接给属性，故两者都要认
        if frames is None:
            if str(arm) == WORLD_ARM:
                return IDENTITY.copy()
            raise RpentError(
                f"arm {arm!r} 需要 world 外参（{WORLD_ALIAS} 的 T_world_base）才能把 world 坐标换算到"
                f"该臂基座系，但当前没有标定产物——先标定，或改用 arm={WORLD_ARM!r}",
                kind="uncalibrated",
            )
        try:
            return np.asarray(frames.world_from_base(str(arm)), dtype=np.float64)
        except FrameError as exc:
            raise RpentError(str(exc), kind="uncalibrated") from exc

    def _point_in_arm_base(self, adapter, arm: str, point: np.ndarray) -> np.ndarray:
        """``world`` 点 → **该臂基座系**（``look_at`` 的指向 / ``move_to`` 的位置都在臂自己的系里比）。"""
        return transform_points(invert_transform(self._world_from_base(adapter, arm)), point)

    def _set_gripper(self, *args, arm: str | None = None, open: Any = True, settle: Any = None) -> dict:
        """``env.set_gripper``：夹爪开合 —— 只改夹爪槽，其余维**保持目标**（关节 qpos）。

        基座是**目标 qpos**（``observations/action``）而不是实测（见 ``_qpos_base``）：只改夹爪不该
        把手臂目标拉回「实测当前」——那会把 MIT 稳态误差写进目标、并抹掉还没走完的运动。
        回执 ``source`` 标注基座实际来源（``target`` / ``measured``）。
        """
        if args:  # RPent 的 dual_franka 形态：set_gripper(arm, open=...)
            arm = args[0] if arm is None else arm
        self._view.require_adapter()  # 未绑定 adapter：与其余写方法一致地报 state 错
        arms = self._view.arms
        indices = self._arm_indices(arm, arms)
        stride = self._qpos_stride(ActionSpace.JOINT)
        base, source = self._qpos_base(ActionSpace.JOINT, self._view.frame())
        if base is None:
            raise RpentError(
                "no joint qpos in observations (read observations/action or observations/qpos)",
                kind="unsupported",
            )
        target = np.array(base, dtype=np.float32, copy=True)
        wanted = 1.0 if bool(open) else 0.0
        for index in indices:
            target[index * stride + stride - 1] = wanted  # 夹爪槽 = 每臂末位
        reply = {"ok": True, "arm": arm, "open": bool(open), "source": source, "target": target}
        return {**reply, **self._send_qpos(target, ActionSpace.JOINT, indices, settle)}

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
        self._push_qpos(row, space)
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
            if not self._push_qpos(row, space, allow_refusal=True):
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
        self,
        arm: str | None,
        delta: np.ndarray,
        *,
        what: str,
        settle: Any = None,
        vector: np.ndarray | None = None,
        extra: dict | None = None,
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
        action_space = ActionSpace.POSE_DELTA
        if not self._view.supports(action_space):
            raise RpentError(
                f"adapter does not support {action_space.value} actions; {what} unavailable "
                "(robot side must declare pose_delta: 增量叠加在关节段目标上)",
                kind="unsupported",
            )
        stride = self._qpos_stride(action_space)
        indices = self._arm_indices(arm, arms)
        vector = np.asarray(vector, dtype=np.float32).reshape(-1)  # 已逐臂换算好的基座系增量
        frame = self._view.frame()
        before = self._qpos(ActionSpace.POSE, frame=frame, source=SOURCE_TARGET)
        predicted = None if before is None else self._add_pose_delta(before, vector, stride)
        predicted_source = "predicted" if predicted is not None else None
        reply = {
            "ok": True,
            "arm": arm,
            "delta": delta,
            "action_space": action_space.value,
            "states": self._state_vector(frame),
            **(extra or {}),
        }
        if self._dry_run:  # 只回预测的绝对目标，不碰机器人
            return {**reply, "target": predicted, "target_source": predicted_source, **self._dry_run_receipt()}
        self._push_qpos(vector, action_space, with_gripper=False)
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
        stride = self._qpos_stride(ActionSpace.POSE)
        if predicted is not None and not self._pose_values_differ(predicted, before, stride):
            return before, {}  # 增量恒为 0：目标未变，直接拿快照当参考
        config = self._settle_config.with_override(override)
        deadline = time.monotonic() + max(config.target_wait_s, config.poll_s)
        while True:
            current = self._qpos(ActionSpace.POSE, frame=self._view.frame(), source=SOURCE_TARGET)
            if current is not None and self._pose_values_differ(current, before, stride):
                flags: dict = {}
                if predicted is not None and self._pose_targets_differ_beyond(current, predicted, stride, config):
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
            if self._view.value_dim(ActionSpace.POSE) <= 0:
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

        当前值走 :meth:`_qpos`（每轮询都取当前帧的**实测** qpos，含夹爪槽）且**每次轮询取当前帧**，
        所以 MIT 的稳态误差会被如实计入，不会把「下发过目标」当成「已到位」。
        """
        self._view.require_adapter()  # 未绑定 adapter → state 错
        stride = self._qpos_stride(space)
        source = space if space is ActionSpace.POSE else ActionSpace.JOINT
        current = self._qpos(source, frame=self._view.frame())
        if current is None or stride <= 0:
            return None
        if source is ActionSpace.POSE:
            value_dim = self._view.value_dim(ActionSpace.POSE)
            if value_dim < 6:
                return None
            pos_err = 0.0
            rot_err = 0.0
            for index in indices:
                observed = current[index * stride : index * stride + value_dim]
                wanted = target[index * stride : index * stride + value_dim]
                if observed.size != value_dim or wanted.size != value_dim:
                    return None
                pos_err = max(pos_err, float(np.linalg.norm(np.asarray(wanted[:3]) - observed[:3])))
                rot_err = max(rot_err, float(np.linalg.norm(wrap_angles(np.asarray(wanted[3:6]) - observed[3:6]))))
            return max(pos_err, rot_err), pos_err, rot_err
        worst = 0.0
        for index in indices:
            observed = current[index * stride : (index + 1) * stride]
            wanted = target[index * stride : (index + 1) * stride]
            if observed.size != wanted.size:
                return None
            worst = max(worst, float(np.max(np.abs(wanted - observed))))
        return worst, None, worst

    def _qpos_stride(self, space: ActionSpace) -> int:
        """该空间下一条 **qpos** 的每臂宽度（= 值维数 + 夹爪 1）；空间未声明 → ``0``。

        qpos = 每臂「值 + 夹爪」交错拼装（``observations/qpos`` / ``action`` 即此形态）：值段是真正的
        控制内容（``value_dim``），夹爪是每臂末位；``gripper`` 空间的值段**就是**夹爪，故无 ``+1``。
        未声明时返回 ``0``（而不是 ``1``）：否则会把值段的第 0 维当夹爪槽读出来。
        """
        value_dim = self._view.value_dim(space)
        if value_dim <= 0:
            return 0
        return value_dim if space is ActionSpace.GRIPPER else value_dim + 1

    def _arm_indices(self, arm: str | None, arms: list[str]) -> list[int]:
        """``arm`` → 臂下标列表（``None`` = 全部启用臂；未知臂名 → 拒绝）。"""
        if arm is None or arm == "":
            return list(range(len(arms) or 1))
        name = str(arm).strip().lower()
        if name not in arms:
            raise RpentError(f"unknown arm {arm!r} (enabled: {arms})", kind="argument")
        return [arms.index(name)]

    def _send_qpos(self, target: np.ndarray, space: ActionSpace, indices: list[int], settle: Any) -> dict:
        """写原语尾部：下发一条 **qpos** 并等到位 → ``{"sent": …}`` + settle 回执。

        ``dry_run`` 与「已下发」两种形态在这里统一（写原语不必各自铺一遍 dry-run 回执）。
        """
        if self._dry_run:
            return self._dry_run_receipt()
        return {"sent": self._push_qpos(target, space), **self._settle_action(target, indices, space, settle)}

    def _push_qpos(self, qpos, space: ActionSpace, *, allow_refusal: bool = False, with_gripper: bool = True) -> bool:
        """下发一条 **qpos**：按 ``layout`` 一处下发（遥操作中被拒 → ``False``）。

        ``layout`` = ``"<space>+gripper"``：值段与夹爪段在**同一控制拍**写入（拆成两条做不到
        原子，会留下「夹爪已更新、关节还是上一步」的中间态）。

        ``dry_run`` 下**绝不碰 adapter**：各写方法已在前头早返回并给出 ``sent: false`` 回执，
        这里是兜底——真走到这里说明有路径漏了，响亮报错而不是默默把机器人动了。

        ``with_gripper=False``：只发值段（``pose_delta`` 用——增量原语不动夹爪，qpos 的夹爪槽只是
        占位；顺手把实测夹爪当绝对目标重发会把未走完的夹爪目标抹回实测值）。
        """
        if self._dry_run:
            raise RpentError(
                "dry_run is enabled: this path would send a real action (refusing to move the robot)",
                kind="state",
            )
        adapter = self._view.require_adapter()
        vector = np.asarray(qpos, dtype=np.float32).reshape(-1)
        stride = self._qpos_stride(space)
        count = max(len(self._view.arms), 1)
        if stride <= 0 or vector.size != stride * count:
            raise RpentError(
                f"action dim {vector.size} != {space.value} qpos layout ({stride} per arm × {count})",
                kind="argument",
            )
        # qpos 本身就是「每臂块 = 值段 + 夹爪」的排列 → 原样就是 ``<space>+gripper`` 的动作序列
        blocks = vector.reshape(count, stride)
        if space is ActionSpace.GRIPPER:
            layout, action = space.value, blocks[:, -1].copy()
        elif with_gripper:
            layout = f"{space.value}{VALUE_LAYOUT_SEPARATOR}{ActionSpace.GRIPPER.value}"
            action = vector
        else:
            layout, action = space.value, blocks[:, : stride - 1].reshape(-1).copy()
        try:
            sent = adapter.rollout(action, layout=layout) is not False
        except ValueError as exc:  # 维度 / 布局不符：不静默补齐
            raise RpentError(str(exc), kind="argument") from exc
        if not sent and not allow_refusal:
            raise RpentError("robot refused the action (teleop / human takeover in progress)", kind="state")
        return sent

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

    def _state_vector(self, frame: dict) -> np.ndarray | None:
        """状态向量（RPent 的 ``wrapped_state_vector``）= **实测 qpos**（每臂「值 + 夹爪」交错）。

        取不到（机器人不发布该键 / 未声明 joint 空间）才退原始 ``observations/qpos``，不编造；
        ``frame`` 由调用方给定（与同一拍的状态 / 图像同源）。
        """
        vector = self._qpos(ActionSpace.JOINT, frame=frame)
        return as_float_array(frame.get(KEY_QPOS)) if vector is None else vector

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
        布局未覆盖的 edge 启用臂用**当前目标**回填：位姿取 ``observations/pose_target``（= ``action``
        关节段的位姿投影）、夹爪槽取 ``observations/action`` 的每臂末位（**同一条目标向量的两种投影**），
        缺键才整体回落实测（``pose`` + ``qpos`` 夹爪槽）。保持的是**目标**而不是实测：MIT 有稳态误差，
        拿实测回填会把误差写进新目标，逐 chunk 累积。需 ``pose_dim > 0``。
        """
        self._view.require_adapter()  # 未绑定 adapter → state 错（与其余路径一致）
        layout_arms = resolve_layout(self._action_layout)
        edge_arms = self._view.arms
        if self._view.value_dim(ActionSpace.POSE) <= 0:
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
        stride = self._qpos_stride(ActionSpace.POSE)
        base = None
        if any(arm not in covered for arm in edge_arms):  # 未覆盖的臂：保持**当前目标**（不是实测）
            base, _source = self._qpos_base(ActionSpace.POSE, self._view.frame())
        frames = []
        for row in matrix:
            target = (
                np.array(base, dtype=np.float32, copy=True)
                if base is not None
                else np.zeros(stride * len(edge_arms), dtype=np.float32)
            )
            for index, arm in enumerate(layout_arms):
                if arm not in covered:
                    continue
                offset = index * block
                slot = edge_arms.index(arm) * stride
                target[slot : slot + 3] = row[offset : offset + 3]
                target[slot + 3 : slot + 6] = matrix_to_rpy(rot6d_to_matrix(row[offset + 3 : offset + 9]))
                target[slot + stride - 1] = rpent_gripper_to_edge(row[offset + 9])
            frames.append(target)
        return np.asarray(frames, dtype=np.float32)
