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

import numpy as np

from motrix_edge.policy.contract import POLICY_KEY_ACTION_LAYOUT, normalize_action_layout


def _string_map(config: dict, key: str) -> dict[str, str]:
    """Normalize ``Edge name -> inference feature name`` policy mappings."""
    raw = config.get(key) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"policy.{key} must be a mapping of edge name -> inference feature name")
    if any(not isinstance(source, str) or not isinstance(target, str) for source, target in raw.items()):
        raise ValueError(f"policy.{key} must map string names to string names")
    mapping = {source.strip(): target.strip() for source, target in raw.items()}
    if any(not source or not target for source, target in mapping.items()):
        raise ValueError(f"policy.{key} names must be non-empty")
    return mapping


class BasePolicyClient:
    """推理策略客户端基类（策略侧最小接口）。

    与 RobotAdapter 基类一致：以 NotImplementedError 定义抽象接口，
    生命周期由 InferSession（会话）驱动：session_start 时 connect，session_finish 时 disconnect。

    ``requires_prompt``：该策略是否**需要文本指令（prompt）**——语言条件策略（openpi）为
    ``True``，推理前必须已预置非空文本（策略配置项 ``prompt``；会话据此门控）；
    非语言条件策略（lerobot-act：ACT 不接受文本条件）为 ``False``，不参与 prompt 门控。

    ``action_layout``（策略配置项）：策略**输出**的动作语义，决定会话走哪条下发通路——
    ``joint``（缺省）= 每臂 6 关节角，夹爪经独立的 ``gripper`` 空间；``joint+gripper`` = 每臂
    「6 关节角 + 1 夹爪」→ ``adapter.rollout(layout="joint+gripper", arms=...)`` 一次下发所选臂。取值与语义见
    ``motrix_edge.policy.contract``。
    """

    requires_prompt: bool = False  # 是否需要 prompt（语言条件策略子类覆盖为 True）

    def __init__(self, policy_config: dict) -> None:
        self.policy_config = policy_config or {}
        self.server_metadata: dict = {}
        # 动作布局（策略配置项；缺省 joint）：非法取值在**进入会话时**就报错，不拖到每步下发。
        self.action_layout = normalize_action_layout(self.policy_config.get(POLICY_KEY_ACTION_LAYOUT))
        self.joint_offsets = {
            str(arm): np.asarray(offset, dtype=float)
            for arm, offset in (self.policy_config.get("joint_offsets") or {}).items()
        }
        if self.joint_offsets:
            for arm, offset in self.joint_offsets.items():
                if not arm.strip() or offset.shape != (6,) or not np.all(np.isfinite(offset)):
                    raise ValueError(f"policy.joint_offsets.{arm} must contain exactly 6 finite numbers")
        self.gripper_offsets = {
            str(arm): float(offset) for arm, offset in (self.policy_config.get("gripper_offsets") or {}).items()
        }
        if self.gripper_offsets:
            for arm, offset in self.gripper_offsets.items():
                if not arm.strip() or not np.isfinite(offset):
                    raise ValueError(f"policy.gripper_offsets.{arm} must be a finite number")
        self.camera_map = _string_map(self.policy_config, "camera_map")
        self.arm_map = _string_map(self.policy_config, "arm_map")
        self._adapter_cameras: list[str] = []
        self._adapter_arms: list[str] = []
        self._state_dim_per_arm: int | None = None
        # 文本指令（prompt）：**仅语言条件策略（``requires_prompt=True``，如 openpi）使用**——
        # 推理前必须非空（InferSession 门控），录制 rollout 时作 episode 的 task_name。
        # 非语言条件策略（lerobot-act）不使用 prompt：保持 None，不参与门控、不下发。
        # 运行时经 ``infer config set``（POST /v1/infers/config）更新。
        self.prompt = None
        # **实测块长**（策略返回的动作块步数；None = 尚未观测到）：服务端不一定声明块长（openpi
        # 官方 metadata 无 action_horizon；lerobot-act 的 actions_per_chunk 只是请求值），故块长
        # 以实测为准——子类拿到块后经 ``_note_chunk_len`` 回填，RTCManager 据此兜底校准块长上限 H
        # （见 ``rtc.manager.RTCManager.calibrate``）。
        self.observed_chunk_len: int | None = None
        # 本连接是否**真正取到过一块**（``_note_chunk_len`` 置位）：预热据此决定还要不要补一次
        # ``infer_chunk`` —— lerobot-act 构造时就用 ``actions_per_chunk`` 填了 ``observed_chunk_len``
        # （请求值、非实测），只看 ``observed_chunk_len`` 会把「加载完 checkpoint 但没取过块」
        # 误判成已预热（首个 rollout 仍要等 10s 级取块）。**连接级**状态：重连后由子类复位。
        self.chunk_seen = False

    @property
    def connected(self) -> bool:
        """是否已建立到推理节点的连接。子类实现（如基于 ``transport.connected``）。"""
        return False

    def connect(self):
        """建立连接（可安全重复 / 重连）：初始化传输、读取服务端 metadata。"""
        raise NotImplementedError("Subclasses should implement this method.")

    def ensure_connected(self):
        """惰性连接：未连接则 ``connect()``，已连 no-op（会话 ``warmup_required=false`` 时 rollout 触发）。"""
        if not self.connected:
            self.connect()

    def prepare(self, observation=None):
        """可选预热（下发策略指令 / 触发服务端模型加载）；无实现 → no-op。

        **契约（所有策略客户端一致）**：未连接 / 无观测 → **no-op、不抛错**——预热是「尽量提前
        把服务端热起来」的优化，失败不致命（会话记 ``warmup_error``，不中断；首个 ``infer_chunk``
        会重试）。未连接的**推理**才报错（``infer_chunk``）：服务端会静默忽略 Ready 之前下发的
        策略指令，不该让人误以为已就绪。

        需要观测以确定 state 维度 / 相机；由会话的预热（``infer connect``：连接 + prepare +
        取一块丢弃）调用，避免首次 ``infer rollout`` 卡在加载模型上。
        """
        pass

    def prepare_observation(self, observation: dict) -> dict:
        """Capture per-control-tick policy state and return an inference snapshot.

        Stateless policies return the observation unchanged. History-aware clients
        override this hook so RTC requests carry a snapshot aligned with the tick that
        scheduled them, rather than sampling mutable history later in a worker thread.
        """
        return observation

    def observation_ready(self, observation: dict) -> bool:
        """Whether a prepared observation may be submitted to the inference engine."""
        return True

    def bind_adapter(self, action_dim=None, camera_names=None, arm_names=None, state_dim_per_arm=None):
        """Bind Edge-global observation availability and validate the policy subset.

        Adapter configuration controls what Edge can observe. ``camera_map`` / ``arm_map`` then
        select a policy-local subset and map it to inference-side feature names.
        """
        del action_dim  # compatibility input; state slicing uses the explicit per-arm width
        self._adapter_cameras = [str(name).strip() for name in (camera_names or [])]
        self._adapter_arms = [str(name).strip() for name in (arm_names or [])]
        self._state_dim_per_arm = int(state_dim_per_arm) if state_dim_per_arm else None
        missing_cameras = [name for name in self.camera_map if name not in self._adapter_cameras]
        missing_arms = [name for name in self.arm_map if name not in self._adapter_arms]
        if missing_cameras:
            raise ValueError(
                f"policy.camera_map requests unavailable Edge camera(s) {missing_cameras}; "
                f"available: {self._adapter_cameras}"
            )
        if missing_arms:
            raise ValueError(
                f"policy.arm_map requests unavailable Edge arm(s) {missing_arms}; available: {self._adapter_arms}"
            )
        available_offset_arms = self.rollout_arms or []
        missing_offset_arms = [
            name for name in {*self.joint_offsets, *self.gripper_offsets} if name not in available_offset_arms
        ]
        if missing_offset_arms:
            raise ValueError(
                f"policy offsets request unavailable policy arm(s) {missing_offset_arms}; "
                f"available: {available_offset_arms}"
            )

    @property
    def rollout_arms(self) -> list[str] | None:
        """Policy output arm order; absent map keeps the adapter-compatible legacy scope."""
        return list(self.arm_map) or (list(self._adapter_arms) or None)

    def state_for_arms(self, values, arms: list[str] | None = None) -> np.ndarray:
        """Select equal-width arm blocks from an adapter-global flattened state vector."""
        array = np.asarray(values, dtype=np.float32).reshape(-1)
        selected = arms or self.rollout_arms
        if not selected or selected == self._adapter_arms:
            return array
        if not self._adapter_arms or not self._state_dim_per_arm:
            raise ValueError("policy arm selection requires adapter arm names and state_dim_per_arm")
        expected = len(self._adapter_arms) * self._state_dim_per_arm
        if array.size != expected:
            raise ValueError(f"adapter state size {array.size} != {expected} for arms {self._adapter_arms}")
        blocks = array.reshape(len(self._adapter_arms), self._state_dim_per_arm)
        return np.concatenate([blocks[self._adapter_arms.index(arm)] for arm in selected])

    def joint_offsets_for_arms(self, arms: list[str] | None = None) -> np.ndarray | None:
        """Flatten configured final-joint offsets in the action's policy arm order."""
        if not self.joint_offsets:
            return None
        selected = arms or self.rollout_arms
        if not selected:
            raise ValueError("policy.joint_offsets requires named rollout arms")
        return np.concatenate([self.joint_offsets.get(arm, np.zeros(6, dtype=float)) for arm in selected])

    def gripper_offsets_for_arms(self, arms: list[str] | None = None) -> np.ndarray | None:
        """Flatten configured final-gripper offsets in the action's policy arm order."""
        if not self.gripper_offsets:
            return None
        selected = arms or self.rollout_arms
        if not selected:
            raise ValueError("policy.gripper_offsets requires named rollout arms")
        return np.asarray([self.gripper_offsets.get(arm, 0.0) for arm in selected], dtype=float)

    def infer_chunk(self, observation: dict, index: int | None = None):
        """输入观测，返回**一次推理的原始动作块**（策略唯一职责：取推理结果）。

        返回 ``ActionChunk``（``[H, dim]`` + 首步绝对步号）或裸 ndarray（``[H, dim]`` /
        单步 ``[dim]``），空 / 失败返回 ``None`` 供上层跳过。

        ``index`` = 当前**绝对步号**（``RTCManager`` 传入）：需要按步号组织请求的流式策略
        （lerobot-act 的 ``TimedObservation.timestep``）使用；其余策略（openpi）忽略。

        **块缓存 / 三元切分 / 时序平滑 / 预取时机由 ``motrix_edge.rtc`` 统一负责**，策略
        实现里不再有游标、timestep 缓存与重叠聚合（见 wiki/design/motrix_edge_rtc.md）。
        """
        raise NotImplementedError("Subclasses should implement this method.")

    def _note_chunk_len(self, chunk) -> None:
        """记录一次**实测块长**（子类拿到块后调用）：空块 / ``None`` 忽略。

        块长是**契约性信息**（RTC 的块长上限 H、预取提前量 P + S 都以它为准）：服务端不声明
        （openpi 官方 metadata 无 action_horizon）或只声明请求值（lerobot-act 的
        ``actions_per_chunk``）时，只能实测回填——首次拿到块（openpi 的预热那一块）即可确定。
        """
        height = getattr(chunk, "height", None)
        if height:
            self.observed_chunk_len = int(height)
            self.chunk_seen = True

    def reset(self):
        """复位策略状态（如服务端会话状态）。动作块缓存由 RTCManager 负责，不在此。"""
        pass

    def disconnect(self):
        """释放连接资源。"""
        pass
