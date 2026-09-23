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

"""HttpShmAdapter —— HTTP + 共享内存中间件 adapter 的共享基类。

面向「受控子进程」机器人：硬件初始化与连接由独立 SDK 进程自维护，Edge 侧 adapter 只做
**薄客户端**，经两条通道通信：

- **HTTP 指令下行**：``execute`` / ``rollout`` / ``safe_stop`` / ``reset`` /
  ``set_teleop`` / 采集回合控制；
- **共享内存观测上行**：读取关节角 / 夹爪 / 实测位姿 / 目标位姿（``observations/qpos`` /
  ``observations/gripper`` / ``observations/pose`` / ``observations/pose_target``）与 raw RGB 相机帧，
  编码为 Edge 契约的 JPEG；
- **状态查询**：``health`` 实时查询进程，``capture_status`` 查询采集状态（运行位 / 元信息 / 数据目录）。

**子类只需声明类常量**（身份 / 能力 / 连接参数 / 臂布局 / 相机布局），本基类提供全部
通用实现（``__init__`` + 指令 / 观测 / 状态方法）。身份与连接参数由 discover 解析传入
（``name`` / ``endpoint`` / ``shm_name``，缺省回退类常量）；能力由类级常量定义；运行时可由
Edge 配置（``adapter`` 段）裁剪——``configure()`` 只启用指定臂 / 相机，未启用臂动作用
``HOME[space]`` 填充（同空间，不会拿关节值当位姿发）。

**位姿（末端）约定**：下发的位姿目标与读到的位姿（``observations/pose`` /
``observations/pose_target``）**必须同坐标系**（``POSE_FRAME``，如 ``flange`` / ``tcp``）。位姿永远按
``xyz(3) + rpy(3)`` 每臂 6 维、单位**米 / 弧度**（与 edge 契约一致；SDK 若有 0.001mm / 0.001° 之类的
整数标度，由机器人进程换算）。数值明显离谱（量纲写错）时记 ERROR，不把可疑数据喂给策略。
"""

import cv2
import httpx
import numpy as np

from motrix_edge.adapter.base import (
    CAMERA_PREFIX,
    KEY_ACTION,
    KEY_GRIPPER,
    KEY_POSE,
    KEY_POSE_TARGET,
    KEY_QPOS,
    Action,
    ActionSpace,
    CaptureStatus,
    HealthStatus,
    RobotAdapter,
    RobotCapabilities,
)
from motrix_edge.adapter.http_contract import (
    FIELD_ACTION,
    FIELD_ACTION_SPACE,
    FIELD_CONTROL_HZ,
    FIELD_DATA_DIR,
    FIELD_DETAIL,
    FIELD_MEASURED_HZ,
    FIELD_META,
    FIELD_OK,
    FIELD_RUNNING,
    FIELD_TELEOP_ENABLED,
    FIELD_TELEOP_MODE,
    PATH_CAPTURE_END,
    PATH_CAPTURE_START,
    PATH_CAPTURE_STATUS,
    PATH_CAPTURE_SYNC,
    PATH_EXECUTE,
    PATH_HEALTH,
    PATH_RESET,
    PATH_ROLLOUT,
    PATH_SAFE_STOP,
    PATH_TELEOP,
)
from motrix_edge.adapter.shm_contract import ObsShmReader
from motrix_edge.utils.data_handler import debug_print

# 位姿量纲防护阀值：明显超出这些范围的位姿视为「量纲 / 坐标系写错」（如把 0.001mm 整数
# 直接当米 → 1e5 量级），丢弃该拍位姿并记 ERROR，不喂给策略 / planner。
POSE_MAX_ABS_XYZ_M = 10.0  # 位置：米（工作站半径远小于此）
POSE_MAX_ABS_RPY_RAD = 7.0  # 姿态：弧度（约 2π，留余量）


def _as_opt_float(value) -> float | None:
    """health 频率字段：缺失 / 非数值 → None。"""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class HttpShmAdapter(RobotAdapter):
    """HTTP + 共享内存中间件 adapter（子类声明类常量，行为全在此）。"""

    def __init__(self, name: str = "", *, endpoint: str | None = None, shm_name: str | None = None):
        """中间件实例：身份与连接参数由 discover 赋予，缺省回退类常量。

        - ``name``：机器人进程 discover 解析出的名称（展示用；缺省回退类常量）。
        - ``endpoint`` / ``shm_name``：**进程自报**的连接参数（HTTP 指令地址 / 观测共享
          内存名）——discover 成功即可直接指令下发，不必与类常量保持一致；缺省（无
          discover，如进程内测试）回退类常量 ``SDK_URL`` / ``SHM_NAME``。
        - ``type`` 由类常量 ``ADAPTER_TYPE`` 确定（entry point 类型，用于实例化）。
        - 能力（动作维度 / 相机布局）由类级常量定义；运行时经 ``configure()`` 应用 Edge
          配置（启用臂 / 相机）。
        """
        super().__init__(name=name, endpoint=endpoint, shm_name=shm_name)
        self.name = name or self.NAME
        # 能力：类级常量（自包含，不随 discover 传输）；基类 __init__ 已初始化
        # enabled_arms / 各空间维度（action_dims）/ _home / _full_image_names
        self.images = list(self.IMAGES)  # 相机名列表（IMAGES 字典的键；configure 可裁剪）
        self.robot_model_id = self.ROBOT_MODEL_ID
        self.robot_model_version = self.ROBOT_MODEL_VERSION
        self._capabilities = dict(self.CAPABILITIES)

        # 中间件连接参数：进程自报优先，缺省回退类常量
        self.sdk_url = (endpoint or self.SDK_URL).rstrip("/")
        self.shm_name = shm_name or self.SHM_NAME
        self.http_timeout = self.HTTP_TIMEOUT

        # 惰性连接资源：首次指令 / 观测时建立（SDK 自维护硬件与连接）
        self._http: httpx.Client | None = None  # SDK HTTP 客户端（指令下行）
        self._shm: ObsShmReader | None = None  # 共享内存观测读者（观测上行）
        self._running = False  # 机器人进程最近一次确认是否运行（health 实时刷新）
        self._pose_problem_logged: str | None = None  # 位姿不可用原因（同原因只记一条）

        # 本地记录（便于调试与无硬件测试）
        self.executed: list[list[float]] = []
        self.rollout_calls = 0
        self.safe_stop_calls = 0
        self.reset_calls = 0
        self.teleop_enabled = False
        self.teleop_mode: str | None = None  # 最近一次 set_teleop 的模式（None = 未指定，进程侧缺省）
        self.rollout_refused_calls = 0  # 遥操作（人工接管）中 rollout 被 SDK 拒绝（409）次数
        self._rollout_refused_logged = False  # 拒绝日志限流位（仅状态变化时各记一条）

    @property
    def running(self) -> bool:
        """机器人进程最近一次确认是否运行（health 实时刷新）。"""
        return self._running

    def _client(self) -> httpx.Client:
        """惰性建立 SDK HTTP 客户端（首次指令 / 查询时）。"""
        if self._http is None:
            self._http = httpx.Client(base_url=self.sdk_url, timeout=self.http_timeout)
        return self._http

    def release(self):
        """释放 Edge 侧本地资源（SDK 连接由进程自维护）。"""
        if self._shm is not None:
            self._shm.close()
            self._shm = None
        if self._http is not None:
            self._http.close()
            self._http = None
        debug_print(self.name, f"{type(self).__name__} released.", "INFO")

    # ---- capabilities ----------------------------------------------------------
    @property
    def capabilities(self) -> RobotCapabilities:
        # 状态键在前（qpos → action → gripper → 位姿实测 / 目标，与 SHM 布局顺序一致），相机键在后
        obs_keys = [KEY_QPOS, KEY_ACTION, KEY_GRIPPER]
        if self.effective_pose_dim_per_arm() > 0:
            # 机器人提供位姿才声明（否则观测里确实没有这两个键）
            obs_keys.append(KEY_POSE)
            obs_keys.append(KEY_POSE_TARGET)
        obs_keys += [f"{CAMERA_PREFIX}{img}" for img in self.images]
        return RobotCapabilities(
            robot_model_id=self.robot_model_id,
            robot_model_version=self.robot_model_version,
            action_dim=self.action_dims.get(ActionSpace.JOINT.value, 0),
            action_dims=dict(self.action_dims),
            action_spaces=[space.value for space in self.ACTION_SPACES],
            observation_keys=obs_keys,
            capabilities=dict(self._capabilities),
        )

    # ---- health（实时查询 SDK 进程状态）-----------------------------------------
    def health(self) -> HealthStatus:
        """健康检查：实时 ``GET /v1/health``（SDK 自维护硬件；Edge 只查询）。

        附带 robot 名义 / 实测控制频率（control_hz / measured_hz，robot env 上报）。
        """
        data = {}
        try:
            resp = self._client().get(PATH_HEALTH)
            data = resp.json() if resp.status_code == 200 else {}
            ok = resp.status_code == 200 and bool(data.get(FIELD_OK, False))
        except Exception as exc:  # noqa: BLE001 进程失联
            debug_print(self.name, f"health check failed: {exc}", "WARNING")
            ok = False
        self._running = ok
        return HealthStatus(
            ok=ok,
            detail=str(data.get(FIELD_DETAIL) or ""),
            control_hz=_as_opt_float(data.get(FIELD_CONTROL_HZ)),
            measured_hz=_as_opt_float(data.get(FIELD_MEASURED_HZ)),
        )

    # ---- 指令（经 HTTP 转发 SDK 进程）-------------------------------------------
    def _require_full_arms_for_cartesian(self, space: ActionSpace, operation: str) -> None:
        """笛卡尔动作的臂裁剪守卫：**要求全部臂在设备端启用**。

        未启用臂在 ``_expand_action`` 里用 ``HOME[space]``（**同空间** home）填充——关节 /
        夹爪都有意义，而 ``pose`` / ``pose_delta`` **没有 home**（编一个位姿才是真危险）：故宁可在此
        直接拒绝（``ValueError``），要求调用方要么启用全部臂、要么退回关节空间。
        """
        if space not in (ActionSpace.POSE, ActionSpace.POSE_DELTA):
            return
        if self.ARM_NAMES and len(self.enabled_arms) != len(self.ARM_NAMES):
            raise ValueError(
                f"{operation} pose requires all arms enabled "
                f"(enabled: {list(self.enabled_arms)}, arms: {list(self.ARM_NAMES)})"
            )

    def reset(self) -> None:
        """程序复位到 home（非阻塞）：HTTP 转发 SDK 进程。"""
        self.reset_calls += 1
        self._client().post(PATH_RESET)

    def execute(self, action: Action, action_space: ActionSpace | str | None = None) -> None:
        """直接下发动作指令（raw）：本地记录 + HTTP 转发 SDK 进程。

        经 ``_expand_action`` 校验维度（启用臂数）并把动作展开回完整空间（未启用臂 home
        填充）再发送；``action_space`` 非关节空间时随 body 下发，由机器人进程解算（笛卡尔
        动作要求全臂启用，见 ``_require_full_arms_for_cartesian``）。
        """
        space = self.normalize_action_space(action_space)
        self._require_full_arms_for_cartesian(space, "execute")
        target = self._expand_action(action, "execute", space)
        self.executed.append(target.tolist())
        body: dict = {FIELD_ACTION: target.tolist()}
        if space is not ActionSpace.JOINT:
            body[FIELD_ACTION_SPACE] = space.value
        debug_print(self.name, f"execute sent: {target.tolist()} space={space.value}", "INFO")
        self._client().post(PATH_EXECUTE, json=body)

    def set_teleop(self, enabled: bool, mode: str | None = None) -> None:
        """设置遥操作（true=遥操作 / false=程控）：本地记录 + HTTP 转发 SDK 进程。

        ``mode``（``absolute`` / ``delta``）: 仅开启时有意义，缺省不发送该字段（进程侧回退
        ``absolute``，与只发 ``enabled`` 的旧调用方等价）。
        """
        self.teleop_enabled = bool(enabled)
        self.teleop_mode = mode if self.teleop_enabled else None
        body: dict = {FIELD_TELEOP_ENABLED: self.teleop_enabled}
        if self.teleop_mode:
            body[FIELD_TELEOP_MODE] = self.teleop_mode
        debug_print(self.name, f"teleop set to {self.teleop_enabled} (mode={self.teleop_mode})", "INFO")
        self._client().post(PATH_TELEOP, json=body)

    def rollout(self, action: Action, action_space: ActionSpace | str | None = None) -> bool:
        """推理闭环：经 ``_expand_action`` 校验 / 展开后 HTTP 转发（SDK 侧设为限速目标并逐帧靠近）。

        ``action_space`` 声明 ``action`` 的语义（缺省 ``joint``，与仅下发关节动作的调用方
        等价）；非关节空间时随 body 下发 ``action_space`` 字段，由机器人进程做 IK。

        遥操作（人工接管）中 SDK 返回 **409**：本拍不下发（返回 False）。持续推理（30Hz）会反复
        命中，故日志**只在状态变化时**各记一条（进入拒绝 / 恢复下发）。
        """
        space = self.normalize_action_space(action_space)
        self._require_full_arms_for_cartesian(space, "rollout")
        target = self._expand_action(action, "rollout", space)
        self.rollout_calls += 1
        body: dict = {FIELD_ACTION: target.tolist()}
        if space is not ActionSpace.JOINT:
            body[FIELD_ACTION_SPACE] = space.value
        resp = self._client().post(PATH_ROLLOUT, json=body)
        if resp.status_code == httpx.codes.CONFLICT:  # 遥操作中：推理让位（见 /v1/rollout 契约）
            self.rollout_refused_calls += 1
            if not self._rollout_refused_logged:
                self._rollout_refused_logged = True
                debug_print(self.name, "rollout refused: robot in teleop (human takeover)", "WARNING")
            return False
        if self._rollout_refused_logged:  # 遥操作结束：恢复下发（状态变化才记日志）
            self._rollout_refused_logged = False
            debug_print(self.name, "rollout resumed (teleop off)", "INFO")
        return True

    def safe_stop(self) -> None:
        """安全停止（幂等、失败安全）：本地记录 + HTTP 转发 SDK 进程。

        语义为**软停**（停发指令、保持位姿、不断电）；断电急停由现场按钮 / 流程负责。
        """
        self.safe_stop_calls += 1
        try:
            self._client().post(PATH_SAFE_STOP)
        except Exception as exc:  # noqa: BLE001 安全停止失败只记录，不覆盖原始故障
            debug_print(self.name, f"safe_stop failed: {exc}", "ERROR")

    # ---- 采集状态（运行位 / 元信息 / 数据落盘）-----------------------------------
    def capture_status(self) -> CaptureStatus | None:
        """采集状态：运行位（是否正在采集）+ 元信息（采集员 / 任务名等）+ 数据目录。"""
        try:
            resp = self._client().get(PATH_CAPTURE_STATUS)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            debug_print(self.name, f"capture status query failed: {exc}", "WARNING")
            return None
        return CaptureStatus(
            running=bool(body.get(FIELD_RUNNING, False)),
            meta=dict(body.get(FIELD_META, {}) or {}),
            data_dir=body.get(FIELD_DATA_DIR),
        )

    def sync_capture_meta(self, meta: dict) -> None:
        """同步采集元信息到 SDK 进程（进程保存一轮数据时附加）。"""
        debug_print(self.name, f"capture meta sync: {meta}", "INFO")
        self._client().post(PATH_CAPTURE_SYNC, json={FIELD_META: meta})

    def start_capture(self) -> None:
        """开始一轮采集：HTTP 转发 SDK 进程（episode 开始）。"""
        debug_print(self.name, "capture episode start", "INFO")
        self._client().post(PATH_CAPTURE_START)

    def end_capture(self) -> None:
        """结束一轮采集：HTTP 转发 SDK 进程（episode 结束）。"""
        debug_print(self.name, "capture episode end", "INFO")
        self._client().post(PATH_CAPTURE_END)

    # ---- observe（共享内存观测上行）---------------------------------------------
    def observe(self) -> dict | None:
        """读取共享内存最新观测帧（SDK 进程产出），图像编码为 JPEG（Edge 契约）。

        只返回启用臂 / 启用相机的观测（configure 裁剪）；SDK 进程把观测填充到共享内存，
        observe 只读取、不推进 SDK 运行。尚无首帧时返回 ``None``。

        状态**各自独立**（都与动作空间无关）：``observations/qpos`` = 关节角、
        ``observations/gripper`` = 夹爪、``observations/pose`` = **实测**位姿、
        ``observations/pose_target`` = **目标**位姿（= ``FK(关节段目标)``，随位姿一起提供）；
        故数采 / 策略拿到的永远是关节角 + 夹爪。
        """
        if self._shm is None:
            self._shm = ObsShmReader(self.shm_name)  # 惰性 attach（首次观测时）
        frame = self._shm.read()
        if frame is None:
            return None  # SDK 尚未产出首帧：瞬态无帧，不是空观测
        obs = {
            KEY_QPOS: np.asarray(
                self._select_arm_segments(frame["qpos"], self.ACTION_DIM_PER_ARM["joint"]), dtype=np.float32
            ),
            # action = 关节段目标（BaseRobot.get_action()；无指令时进程回退 qpos）——不是 qpos 副本：
            # SHM 布局单独带 action 区（见 shm_contract），preview 显示的是真指令
            KEY_ACTION: np.asarray(frame["action"], dtype=np.float32),
            KEY_GRIPPER: self._select_arm_segments(frame["gripper"], 1),
        }
        # 末端位姿（机器人提供时）：按启用臂挑选，与 qpos 同一裁剪口径
        per_arm = self.effective_pose_dim_per_arm()
        pose = frame.get("pose")
        if pose is not None and per_arm > 0:
            problem = self._pose_problem(pose, per_arm)
            if problem is None:
                obs[KEY_POSE] = self._select_arm_segments(pose, per_arm)
            else:
                self._warn_bad_pose(problem)  # 一次一条：不把可疑位姿喂给下游
        # 目标位姿（= FK(关节段目标)）：与实测位姿同拍同源；量纲防护同一套
        target = frame.get("pose_target")
        if target is not None and per_arm > 0:
            problem = self._pose_problem(target, per_arm)
            if problem is None:
                obs[KEY_POSE_TARGET] = self._select_arm_segments(target, per_arm)
            else:
                self._warn_bad_pose(problem)
        # strict=True：路数由两侧各自声明（SHM header 的 image_count ↔ 类常量 IMAGES），
        # 不一致时显式报错，避免静默少一路相机；只暴露 configure 启用的相机
        for image, name in zip(frame["images"], self._full_image_names, strict=True):
            if name in self.images:
                obs[f"{CAMERA_PREFIX}{name}"] = self._encode_jpeg(image)
        return obs

    @staticmethod
    def _encode_jpeg(rgb: np.ndarray) -> bytes:
        """RGB ndarray → JPEG bytes（观测缓存图像编码；SDK 产出原图尺寸）。"""
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        if not ok:
            raise ValueError("Failed to encode image as JPEG")
        return buf.tobytes()

    def _pose_problem(self, pose, per_arm: int) -> str | None:
        """位姿可用性检查：形状对不上 / 数值离谱 → 返回原因（None = 可用）。

        位姿一旦被当米/弧度用（或反过来），下游「当前位姿 + 增量」会直接把机器人指到
        荒谬坐标——宁可不给，也不给可疑值。
        """
        values = np.asarray(pose, dtype=np.float64).reshape(-1)
        expected = (len(self.ARM_NAMES) or 1) * int(per_arm)
        if values.shape[0] != expected:
            return f"pose dim {values.shape[0]} != {expected} (arms × {per_arm})"
        blocks = values.reshape(-1, int(per_arm))
        if blocks.shape[1] < 6:
            return f"pose per-arm dim {blocks.shape[1]} < 6 (xyz + rpy)"
        xyz, rpy = blocks[:, :3], blocks[:, 3:6]
        if not np.all(np.isfinite(values)):
            return "pose contains non-finite values"
        if float(np.max(np.abs(xyz))) > POSE_MAX_ABS_XYZ_M:
            return f"|xyz| up to {float(np.max(np.abs(xyz))):.3g} m > {POSE_MAX_ABS_XYZ_M} (unit mismatch?)"
        if float(np.max(np.abs(rpy))) > POSE_MAX_ABS_RPY_RAD:
            return f"|rpy| up to {float(np.max(np.abs(rpy))):.3g} rad > {POSE_MAX_ABS_RPY_RAD} (unit mismatch?)"
        return None

    def _warn_bad_pose(self, problem: str) -> None:
        """位姿不可用：**一次一条** ERROR（同原因不刷屏），本拍不透传 ``pose``。"""
        if problem == self._pose_problem_logged:
            return
        self._pose_problem_logged = problem
        debug_print(
            self.name,
            f"end-effector pose dropped ({problem}); cartesian primitives / settle will be unavailable",
            "ERROR",
        )
