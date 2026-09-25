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

"""BaseRobot —— 机器人基类（**无 profile**；obs/action 形态由各机器人类常量固定）。

机器人是「会动」的一层：持有 ``action``（当前指令）与 ``target_action``（目标），由运行
环境（env）控制线程每拍调用 ``step()`` 限速接近 target 并执行。本基类**不依赖
motrix_edge.profile**——每个机器人的 obs/action 形态（动作维度 / 相机布局）由子类
**类常量固定声明**，与对应 adapter 协定一致。

约定：
- **扁平动作**：每个动作空间的值都是一维数组、**按臂展开**（每臂等长），维度见
  ``wiki/design/robot_pipeline_action_spaces.md``：``joint`` = 每臂 6 关节、
  ``pose`` / ``pose_delta`` = 每臂 ``xyz + rpy``（前者绝对目标、后者增量）、`gripper` = 每臂 1 夹爪。
  例：双臂 → ``QPOS``(joint) = 12、``POSE`` = 12、``GRIPPER`` = 2。
- **原始观测**：``sample_qpos()`` / ``build_observation()``（单线程调试可用 ``get_observation()``）
  产出 ``{observations/qpos, observations/gripper, observations/pose, observations/pose_target,
  action, timestamp}``——
  ``observations/qpos`` **始终是关节角**（位姿另占两个键：实测 / 目标，均由同一套 FK 给出），与下发
  的是哪个动作空间无关；键名复用 adapter 契约常量（``KEY_QPOS`` / ``KEY_GRIPPER`` / ``KEY_POSE`` /
  ``KEY_POSE_TARGET`` / ``CAMERA_PREFIX`` / ``KEY_TIMESTAMP``）；robot server
  （contract_server）据此组装 standard_obs 并写入共享内存。
- **控制 / 观测分线程（env 双线程序列）**：控制线程每拍 ``step()`` 限速推进 +
  ``sample_qpos()`` 采样机械臂状态（关节角 / 夹爪 / action）缓存进 ``motion_state``；观测线程每拍
  ``build_observation()``（= 缓存状态 + 本拍相机帧），观测帧的 ``timestamp`` 由**观测线程**在打帧时
  写入（不是控制拍的采样时刻）。
  机械臂读取与相机取帧因此分处两线程，相机卡顿只会让观测丢帧，不会拖慢机械臂步进；
  单线程脚本仍可用 ``get_observation()`` 一次取整帧。
- **控制**：``reset()`` / ``execute()`` / ``rollout()`` / ``safe_stop()`` 只修改
  ``target_action``，实际运动由 env 控制线程每拍 ``step()`` 限速推进（单一目标模型）。
- **动作空间**（``action_space``）：``joint``（缺省，每臂 6 关节角）/ ``pose``
  （每臂 ``xyz + rpy``，绝对目标）/ ``pose_delta``（每臂 ``xyz + rpy``，**增量**）/
  ``gripper``（每臂 1 夹爪）——**各空间只表达一件事**，值都按臂展开，
  夹爪不再混在 joint / pose 的值里（见 ``wiki/design/robot_pipeline_action_spaces.md``）。
  ``pose`` / ``pose_delta`` 是**动作语义而非控制模式**——机器人在收到位姿（增量）目标时
  **自己解算成关节角**（调控制器的静态转换函数，如 ``PiperController.pose_to_joint``），
  之后与关节目标走同一条通路（``target_action`` 限速 → ``set_joint``），
  底层控制通路（``set_joint`` → MIT）零改动。``pose_delta`` 的基准是**关节段目标**的正解位姿
  （``get_target_pose()``），不是实测位姿（底层 MIT 无重力前馈，以实测为基准会逐步累积误差）。
  声明见 ``ACTION_SPACES``、实现见具体机器人的 ``_prepare_target()``。
- **遥操作优先于推理**：``teleop_enabled`` 期间 ``rollout()`` **一律被拒绝**（不分模式：
  遥操作即人工接管，推理让位，返回 False）；``execute()`` / ``reset()`` / ``safe_stop()``
  仍是程控 / 安全优先，执行即结束遥操作。
- **遥操作**：``teleop_enabled`` 默认 **False**（adapter 通讯控制中暂时均处于 false）；
  接入位由子类在 ``_get_teleop_target()`` 实现（返回主臂**绝对**扁平读数），``step()`` 仅在
  开启时刷新 ``target_action``。
- **两种遥操作模式**（``teleop_mode``）：``absolute`` 主臂绝对位姿直连从臂 target（示教采集）；
  ``delta``（**人工接管**）``target = slave_ref + (master_now − master_ref)``——锚点在接管后
  首拍采样（主臂读数与从臂位姿同一拍），增量恒从 0 开始，从臂不会因主从位姿差突变；关节与
  夹爪同一套增量语义，逐拍限速仍由 ``step_rad`` 唯一负责。详
  ``wiki/design/robot_pipeline_teleop.md``。
"""

import time

import numpy as np
from utils.base.data_handler import debug_print

from robot.kinematics import wrap_angles


class CartesianActionError(ValueError):
    """位姿动作（每臂 ``xyz + rpy``）不可用 / 解算失败（robot server 映射为 HTTP 422，**不改既有目标**）。"""


class BaseRobot:
    # ---- 身份 / 能力（对齐 adapter 契约；子类覆盖）----
    NAME = "base_robot"
    ADAPTER_TYPE = "base_robot"  # 本机器人对应的 adapter 类型（entry point 名）
    ROBOT_MODEL_ID = "base-robot"
    ROBOT_MODEL_VERSION = "0.0.0"
    CAPABILITIES: dict[str, bool] = {}

    # ---- 布局 / 特性（子类覆盖）----
    # 每个动作空间的值**按臂展开**（每臂等长），维度 = 每臂维数 × 臂数：
    QPOS = 12  # ``joint`` 空间维度（每臂 6 关节角）
    POSE = 12  # ``pose`` / ``pose_delta`` 空间维度（每臂 xyz + rpy）；0 = 本机器人不提供位姿
    POSE_DIM_PER_ARM = 6  # 每臂位姿维数（xyz + rpy）：POSE = 每臂维数 × 臂数
    GRIPPER = 2  # ``gripper`` 空间维度（每臂 1 夹爪）
    IMAGE_NAMES: list[str] = []  # 相机名（observations/images/<name>）
    IMAGES: dict[str, tuple[int, int]] = {name: (640, 480) for name in IMAGE_NAMES}  # 相机名 -> (w, h)
    SHM_NAME = "robot_obs"  # 观测共享内存名（server 侧发布）

    # ---- 臂与取数 / 下发（子类按自己的接线实现）----
    # 机器人只做两件事：**从控制器 / 传感器取数**（``get_observation_qpos`` / ``get_observation_gripper`` /
    # ``get_observation_images``）与**下发目标**（``_apply_action`` / ``_prepare_target``）。
    # 运动学（FK / IK / 限位）都在控制器里（``robot/controller``），本层不碰。

    # ---- 动作空间（取值与 edge 契约 ActionSpace 同名）----
    ACTION_SPACE_JOINT = "joint"  # 每臂 6 关节角（绝对目标）
    ACTION_SPACE_POSE = "pose"  # 每臂 xyz + rpy（**绝对目标**；由控制器求解器解算成全关节目标）
    # 每臂 xyz + rpy（**增量**）：叠加在**当前关节段目标**的正解位姿上再解算——基准不是实测
    # 位姿（底层 MIT 有稳态误差，以实测为基准会把误差写进新目标、逐步累积）。
    ACTION_SPACE_POSE_DELTA = "pose_delta"
    ACTION_SPACE_GRIPPER = "gripper"  # 每臂 1 夹爪（绝对目标，归一化 [0, 1]）
    # 缺省 = 关节 + 夹爪；接入位姿的机器人自行把 ``pose`` / ``pose_delta`` 加进来（按 ``POSE > 0``）
    ACTION_SPACES: tuple[str, ...] = (ACTION_SPACE_JOINT, ACTION_SPACE_GRIPPER)

    # ---- 观测键（adapter 契约约定，与 Edge 侧 / robot server 保持一致；勿改）----
    KEY_QPOS = "observations/qpos"  # 关节角（**始终是关节**，与动作空间无关）
    KEY_GRIPPER = "observations/gripper"  # 夹爪键（独立动作空间，故独立观测键）
    KEY_POSE = "observations/pose"  # 实测末端位姿键（POSE > 0 时写入；每臂 xyz + rpy）
    # **目标位姿** = ``FK(关节段目标)``——底层位姿目标（POSE > 0 时写入，与 KEY_POSE 同一套 FK、
    # 同一拍）：增量动作的解算结果因此对上位可见（判到位不必自己攒基准），「目标 − 实测」即稳态误差。
    KEY_POSE_TARGET = "observations/pose_target"
    KEY_ACTION = "action"  # 关节段目标键（无指令时回退 qpos）
    KEY_TIMESTAMP = "timestamp"  # 观测帧时刻（观测线程打点）
    CAMERA_PREFIX = "observations/images/"  # 相机图像键前缀（<prefix><cam_name>）

    # ---- 遥操作模式（取值与 /v1/teleop 的 mode 同名；子类一般不用覆盖）----
    TELEOP_MODE_ABSOLUTE = "absolute"  # 主臂绝对位姿直连从臂 target（示教采集）
    TELEOP_MODE_DELTA = "delta"  # 锚点增量：target = slave_ref + (master_now − master_ref)（人工接管）
    TELEOP_MODES = (TELEOP_MODE_ABSOLUTE, TELEOP_MODE_DELTA)

    def __init__(self, robot_config: dict | None = None):
        self.robot_config = dict(robot_config or {})
        self.name = str(self.robot_config.get("name", self.NAME))
        # 每帧最大关节增量（rad）：限速插值步长，可经配置 step_rad 修改
        self.step_rad = float(self.robot_config.get("step_rad", 0.1))
        self.ready = False
        self.last_error = None

        # 控制状态：**单条底层向量** ``[关节段 | 夹爪段]``（关节段在前，长度 QPOS / GRIPPER）——
        # 底层始终是关节控制（pose 只在落 target 时解算成关节），夹爪只是同一向量里的另一段，
        # 各动作空间只写自己那一段（见 wiki/design/robot_pipeline_action_spaces.md）。
        #   action / target_action：当前位置 / 目标（reset / execute / rollout 都只覆盖对应段）
        self.action: np.ndarray | None = None
        self.target_action: np.ndarray | None = None
        # 复位目标：关节段（init_joint，必填）+ 夹爪段（init_gripper，缺省全 1 = 张开）
        self.init_joint = self._init_vector("init_joint", self.QPOS, required=True)
        self.init_gripper = self._init_vector("init_gripper", self.GRIPPER, required=False, fill=1.0)

        # 遥操作：开关 + 映射模式 + 增量锚点（锚点在接管后首拍采样，见 _capture_teleop_anchor）
        self.teleop_enabled = False
        self.teleop_mode = self.TELEOP_MODE_ABSOLUTE
        self.teleop_master_ref: np.ndarray | None = None  # 主臂关节锚点（delta 模式）
        self.teleop_slave_ref: np.ndarray | None = None  # 从臂关节锚点（delta 模式，接管瞬间）
        self.teleop_master_gripper_ref: np.ndarray | None = None  # 主臂夹爪锚点（delta 模式）
        self.teleop_slave_gripper_ref: np.ndarray | None = None  # 从臂夹爪锚点（delta 模式）
        # 主臂读数不可用（读不到 / 读取异常）：累计计数 + 限流告警。**不置 last_error**——
        # 接管中读不到主臂是预期状态（人还没接上 / 瞬时抖动），不能让 /v1/health 变不健康；
        # 但必须可观测，否则操作员只看到「遥操作开着、机械臂不动」（见 _note_teleop_read_failure）
        self.teleop_read_failures = 0
        self._teleop_read_error: str | None = None  # 最近一次原因（同一原因只告警一条）
        # 最近一次**成功**的主臂读数（关节 / 夹爪）：供采集侧「帧头跳过」判断主臂是否已有效
        # 移动（``teleop_master_sample()``）；开关遥操作时随锚点一起清空（不留旧样本）
        self.teleop_master_now: np.ndarray | None = None
        self.teleop_master_gripper_now: np.ndarray | None = None

        # 帧计数（get_observation() 每帧自增；server 组装 standard_obs 时附带）
        self.seq = 0

        # 控制器 / 传感器（真实机器人填充；虚拟机器人可为空）
        self.controllers: dict = {}
        self.sensors: dict = {}

        # 机械臂侧状态缓存（控制线程每拍 sample_qpos() 覆盖；观测线程只读）
        self.motion_state: dict | None = None

    # ---- 硬件接线（robot.ports / robot.cameras：**值必填**，代码内不存现场值）----
    def _required_devices(self, section: str, keys: tuple[str, ...]) -> dict[str, str]:
        """读取**必填**的硬件接线 ``robot.<section>.<key>``；缺失 / 空串 → ValueError。

        端口（CAN 接口名 / 串口设备节点）与相机设备（RealSense 序列号 / V4L2 节点）都只在
        配置里给——代码不保存现场值（避免「改了接线忘改代码」），**也不替现场编一个**：没有
        就报错，让问题停在启动时而不是 SDK 连接时。未知键名打 WARNING（发现拼写错误，
        不影响启动）。
        """
        values = self.robot_config.get(section) or {}
        unknown = [key for key in values if key not in keys]
        if unknown:
            debug_print(self.name, f"robot.{section} 未知键 {unknown}（可用：{list(keys)}），已忽略", "WARNING")
        missing = [key for key in keys if not str(values.get(key) or "").strip()]
        if missing:
            raise ValueError(f"配置缺少 robot.{section}：{missing}（必填，无代码缺省值；请按现场接线填写）")
        return {key: str(values[key]).strip() for key in keys}

    # ---- 布局 / 解析（无 profile；obs/action 形态由类常量固定）------------------
    def _init_vector(self, key: str, dim: int, *, required: bool, fill: float = 0.0) -> np.ndarray:
        """读复位向量（「空间名」结尾：``init_joint`` 关节段 / ``init_gripper`` 夹爪段）。

        长度必须等于该空间维度（``QPOS`` / ``GRIPPER``）；夹爪已独立为 ``gripper`` 空间，
        不再拼在关节维度里，故旧的 14 维 ``init_joint`` 在这里就被拦住（在启动期报错，
        而不是等控制拍拿到错长度的目标）。
        """
        raw = self.robot_config.get(key)
        if raw is None:
            if required:
                raise ValueError(f"Robot {self.name} {key} is not set in robot_config.")
            return np.full(dim, float(fill))
        values = np.asarray(raw, dtype=np.float64).reshape(-1)
        if values.shape[0] != dim:
            raise ValueError(
                f"{self.name}: {key} dim {values.shape[0]} != {dim}"
                "（夹爪已独立为 gripper 动作空间，不再拼在关节维度里）"
            )
        return values

    @classmethod
    def action_dim(cls, action_space: str | None = None) -> int:
        """该动作空间的值维度（缺省 ``joint``）：``QPOS`` / ``POSE`` / ``GRIPPER``。

        ``pose_delta`` 与 ``pose`` **同形**（每臂 6 维），差别只在值的语义（增量 vs 绝对）——
        与 ``joint`` / ``pose`` 一样靠空间名区分。
        """
        space = cls.normalize_action_space(action_space)
        return {
            cls.ACTION_SPACE_JOINT: cls.QPOS,
            cls.ACTION_SPACE_POSE: cls.POSE,
            cls.ACTION_SPACE_POSE_DELTA: cls.POSE,
            cls.ACTION_SPACE_GRIPPER: cls.GRIPPER,
        }[space]

    @classmethod
    def action_dims(cls) -> dict[str, int]:
        """本机器人**已声明**的各动作空间维度（按 ``ACTION_SPACES`` 顺序），供能力上报。"""
        return {space: cls.action_dim(space) for space in cls.ACTION_SPACES}

    @classmethod
    def normalize_action_space(cls, action_space: str | None) -> str:
        """校验动作空间名（缺省 / None → ``joint``）；不在 ``ACTION_SPACES`` 内 → ``ValueError``。

        三个空间的维度**各不相同**（joint / pose = 每臂 6，gripper = 每臂 1），所以空间校验与
        维度校验必须成对做（见 ``action_dim()``）——“同一个坐标系里比维度”才是有效的校验。
        """
        space = cls.ACTION_SPACE_JOINT if action_space is None else str(action_space)
        if space not in cls.ACTION_SPACES:
            raise ValueError(f"action space {space!r} not supported (available: {list(cls.ACTION_SPACES)})")
        return space

    # ---- 控制：HTTP（reset/execute/rollout/safe_stop）只修改两段目标 ----------------
    def reset(self):
        """程序复位到 home（非阻塞）：关节段 = ``init_joint``，夹爪段 = ``init_gripper``。

        只覆盖两段目标（关节段 = home、夹爪段 = init_gripper）。
        """
        self.disable_teleop()
        self.set_target_action(self.init_joint)
        self.set_target_gripper(self.init_gripper)

    def execute(self, qpos: np.ndarray, action_space: str | None = None):
        """直接下发动作指令（raw）：按**动作空间**只覆盖自己那一段目标。

        - ``joint``：关节段 = 给定关节角（长度 = ``QPOS``）；
        - ``pose``：**由子类**解算成关节目标（调控制器的静态转换函数）后写关节段；
        - ``pose_delta``：同上，但值是**增量**——由子类叠加在**当前关节段目标**的正解位姿上；
        - ``gripper``：只写夹爪段（不会顺带把关节目标重置）。

        ``joint`` / ``pose`` / ``pose_delta`` 下发只覆盖关节段；``gripper`` 只覆盖夹爪段。
        """
        space = self.normalize_action_space(action_space)
        self.disable_teleop()
        if space == self.ACTION_SPACE_GRIPPER:
            self.set_target_gripper(qpos)
            return
        self.set_target_action(self._prepare_target(qpos, space))

    def rollout(self, action: np.ndarray, action_space: str | None = None) -> bool:
        """推理闭环：**遥操作（人工接管）进行中则拒绝**（返回 False）。

        遥操作开启即视为人工接管（不区分 ``absolute`` / ``delta``）：从臂 target 由人工决定，
        推理下发必须让位——本方法**不改 target、也不退出遥操作**（与 ``execute()`` 的「程控抢回」
        相反），遥操作关闭（``disable_teleop()``）后自动恢复。
        ``action_space`` 语义同 ``execute()``。
        """
        if self.teleop_enabled:
            debug_print(self.name, "rollout refused: teleop (human takeover) active.", "WARNING")
            return False
        self.disable_teleop()
        self.execute(action, action_space)
        return True

    def safe_stop(self):
        """安全停止（幂等、失败安全）：退出遥操作并清空目标，step() 不再推进。

        **本接口是「软停」**：停止下发新目标并保持当前位姿（关节仍带力矩），**不断电**，
        因此机械臂不会失力下垂。硬件急停（断电 e-stop，`PiperController.emergency_stop`）
        **不在这条路径上**——由现场急停按钮 / 作业流程负责；待实现「掉力后受控阻尼下坠」
        流程后再评估是否接入，届时本契约需同步更新。
        """
        self.disable_teleop()  # 退出遥操作（含清空增量锚点）
        self.target_action = None  # 清空整条目标：不再下发新目标（保持当前位姿）
        debug_print(self.name, "Safe stop executed.", "WARNING")

    def _prepare_target(self, qpos: np.ndarray, action_space: str | None) -> np.ndarray:
        """把下发动作归一成**关节段目标**；支持位姿目标的子类覆盖本方法。

        缺省：``joint`` 直通（校验维度 = ``QPOS``）；``pose`` / ``pose_delta`` 未声明 / 未实现 →
        ``CartesianActionError``。子类若声明了位姿空间，在这里把每臂位姿**解算成关节目标**
        （调控制器的静态转换函数）——解算不进控制拍，失败抛错不改既有目标。
        """
        space = self.normalize_action_space(action_space)
        if space != self.ACTION_SPACE_JOINT:
            raise CartesianActionError(
                f"{self.name}: action space {space!r} is not supported (supported: {list(self.ACTION_SPACES)})"
            )
        values = np.asarray(qpos, dtype=np.float64).reshape(-1)
        if values.shape[0] != self.QPOS:
            raise ValueError(f"{self.name}: joint action dim {values.shape[0]} != QPOS {self.QPOS}")
        return values

    # ---- 位姿类动作的公共实现（``pose`` / ``pose_delta``；子类提供 FK / IK 接入）----
    def _absolute_pose_target(self, space: str, values: np.ndarray) -> np.ndarray:
        """位姿类动作 → **绝对目标位姿**（每臂 ``xyz + rpy``，扁平 ``POSE`` 维）。

        - ``pose``：值就是绝对目标，直用（校验维度 / 有限性）；
        - ``pose_delta``：基准是 ``get_target_pose()``（= ``FK(关节段目标)``，**不是实测位姿**），
          ``target = 基准 + [Δxyz, wrap(Δrpy)]``——rpy 与 ``pose`` 同一 chart 相加，小步长下与
          旋转复合等价；基准不可用（未声明位姿 / FK 失败）→ ``CartesianActionError``（不猜）。
        """
        expected = self.POSE_DIM_PER_ARM * len(self.ARM_NAMES)
        if values.shape[0] != expected:
            raise CartesianActionError(
                f"{self.name}: {space} action dim {values.shape[0]} != {expected}"
                f" ({self.POSE_DIM_PER_ARM} per arm × {len(self.ARM_NAMES)} arms)"
            )
        if not np.all(np.isfinite(values)):
            raise CartesianActionError(f"{self.name}: {space} action contains non-finite values")
        if space == self.ACTION_SPACE_POSE:
            return values
        base = self.get_target_pose()
        if base is None or not np.all(np.isfinite(np.asarray(base).reshape(-1)[:expected])):
            raise CartesianActionError(
                f"{self.name}: {space} needs a valid target pose as its base (FK of the joint target)"
            )
        target = np.asarray(base, dtype=np.float64).reshape(-1)[:expected].copy()
        for index in range(len(self.ARM_NAMES)):
            start = index * self.POSE_DIM_PER_ARM
            span = slice(start, start + self.POSE_DIM_PER_ARM)
            target[span] = target[span] + values[span]
            target[start + 3 : start + self.POSE_DIM_PER_ARM] = wrap_angles(
                target[start + 3 : start + self.POSE_DIM_PER_ARM]
            )
        return target

    def _target_joints(self) -> np.ndarray:
        """**关节段目标**（``target_action[:QPOS]``；未下发过指令 → 当前指令位置 ``action``）。

        增量动作的基准与逆解起点都取它（不是实测关节角）：底层 MIT 有稳态误差，用实测会把误差
        写进新目标、逐步累积。
        """
        if self.target_action is not None:
            return np.array(self.target_action[: self.QPOS], dtype=np.float64)
        return np.asarray(self.current_action(), dtype=np.float64)

    def set_target_action(self, target_action: np.ndarray | None):
        """写**关节段**目标（绝对值）：``step()`` 每帧把关节段朝它限速移动。

        ``None`` = 清空整条目标（安全停止：停止下发新目标、保持位姿）。
        增量（人工接管）写入请用 ``set_target_action_delta()``（锚点 + 增量）。
        """
        if target_action is None:
            self.target_action = None
            return
        target = self._ensure_target()
        target[: self.QPOS] = np.asarray(target_action, dtype=np.float64).reshape(-1)

    def set_target_gripper(self, target_gripper: np.ndarray | None):
        """写**夹爪段**目标（绝对值，每臂 1 维）：``step()`` 每拍直接跟随（不插值）。

        维度必须等于 ``GRIPPER``；取值钳到 ``[0, 1]``（归一化行程，钳到时告警一条）。
        """
        if target_gripper is None:
            return  # None = 不改夹爪段（要复位用 reset()）
        values = np.asarray(target_gripper, dtype=np.float64).reshape(-1)
        if values.shape[0] != self.GRIPPER:
            raise ValueError(f"{self.name}: gripper action dim {values.shape[0]} != GRIPPER {self.GRIPPER}")
        clipped = np.clip(values, 0.0, 1.0)
        if not np.allclose(clipped, values):
            debug_print(self.name, f"gripper target clipped to [0, 1]: {values.tolist()}", "WARNING")
        self._ensure_target()[self.QPOS :] = clipped

    def _ensure_target(self) -> np.ndarray:
        """取底层目标向量 ``[关节段 | 夹爪段]``；未初始化时以**当前实际状态**初始化。

        以当前状态（而非 ``init_joint``）初始化，保证「只改一段」的首条命令不会把另一段
        拉回 home（例：安全停止后只发夹爪）。
        """
        if self.target_action is None:
            self.target_action = np.concatenate([self.current_action(), self.current_gripper()])
        return self.target_action

    # ---- 遥操作（模式 / 锚点；adapter 通讯控制中暂时均为 False）-------------------------
    def enable_teleop(self, mode: str = TELEOP_MODE_ABSOLUTE) -> bool:
        """启用遥操作（``mode`` 决定主臂读数 → 从臂 target 的映射）。

        - ``absolute``（缺省）：主臂**绝对**位姿直连从臂 target——主从同构、位姿已对齐的示教采集。
        - ``delta``（**人工接管**）：主臂**增量**映射——``target = slave_ref + (master_now − master_ref)``；
          锚点在启用后的下一拍 ``step()`` 采样（主臂读数与从臂位姿**同一拍**），该拍增量恒 0、
          target = 从臂当时位姿，故接管不会让从臂突变；此后主臂推多少、从臂走多少，主臂回
          锚点则从臂回锚点。关节与夹爪同一套增量语义。**遥操作开启期间 ``rollout()`` 一律被拒**
          （遥操作即人工接管，推理让位），仅 ``execute()`` / ``reset()`` / ``safe_stop()`` 能抢回程控。

        返回是否切换成功（模式名非法 → False，保持原模式与 target 不变）。
        """
        if mode not in self.TELEOP_MODES:
            debug_print(self.name, f"Unknown teleop mode {mode!r} (expect {self.TELEOP_MODES})", "ERROR")
            return False
        self.teleop_mode = mode
        self._clear_teleop_anchor()
        self.teleop_enabled = True
        debug_print(self.name, f"Teleop enabled (mode={mode})", "INFO")
        return True

    def disable_teleop(self):
        """关闭遥操作：回到程序控制，``step()`` 不再刷新 target_action（清空增量锚点）。"""
        if not self.teleop_enabled:
            return  # reset / execute / rollout 每次都会调用：未开启时静默，不刷日志
        self.teleop_enabled = False
        self._clear_teleop_anchor()
        debug_print(self.name, f"Teleop disabled (mode={self.teleop_mode})", "INFO")

    def set_target_action_delta(self, delta: np.ndarray) -> bool:
        """在**增量锚点**（接管瞬间的从臂关节角）上叠加增量，写关节段目标。

        ``target[关节段] = teleop_slave_ref + delta``——增量遥操作写关节段的唯一入口
        （``delta`` = 主臂读数 − 主臂锚点）。``step_rad`` 限速仍由 ``step()`` 完成，本接口
        **不做任何限幅**。锚点未采样时返回 False 且不写 target（本拍保持原目标，不突变）。
        """
        if self.teleop_slave_ref is None:
            return False
        self.set_target_action(self.teleop_slave_ref + np.asarray(delta, dtype=np.float64))
        return True

    def set_target_gripper_delta(self, delta: np.ndarray) -> bool:
        """在**夹爪锚点**上叠加增量（与关节段同一套增量语义）；锚点未采样 → False。"""
        if self.teleop_slave_gripper_ref is None:
            return False
        self.set_target_gripper(self.teleop_slave_gripper_ref + np.asarray(delta, dtype=np.float64))
        return True

    def current_action(self) -> np.ndarray:
        """从臂当前**关节段**（``action`` → ``target_action`` → ``init_joint``）。

        供增量接管采样从臂锚点：接管后 target 从该位姿出发，此前残留（可能已发散）的
        推理 target 被丢弃，从臂动作因此连续。
        """
        if self.action is not None:
            return self.action[: self.QPOS].copy()
        if self.target_action is not None:
            return self.target_action[: self.QPOS].copy()
        return np.asarray(self.init_joint, dtype=np.float64)

    def current_gripper(self) -> np.ndarray:
        """从臂当前**夹爪段**（``action`` → ``target_action`` → ``init_gripper``）。"""
        if self.action is not None:
            return self.action[self.QPOS :].copy()
        if self.target_action is not None:
            return self.target_action[self.QPOS :].copy()
        return np.asarray(self.init_gripper, dtype=np.float64)

    def _capture_teleop_anchor(self, master_joints: np.ndarray, master_gripper: np.ndarray):
        """采样增量接管锚点：主臂读数（关节 + 夹爪）+ 从臂当前值（**同一拍**，即接管瞬间）。

        此后 ``target = slave_ref + (master_now − master_ref)``：增量恒从 0 开始，主从位姿差
        被一次性零化（人工把主臂摆到与从臂相近位姿后接管，二者叠加后动作连续）。
        """
        self.teleop_master_ref = np.asarray(master_joints, dtype=np.float64)
        self.teleop_slave_ref = self.current_action()
        self.teleop_master_gripper_ref = np.asarray(master_gripper, dtype=np.float64)
        self.teleop_slave_gripper_ref = self.current_gripper()
        debug_print(self.name, "Teleop anchor captured (delta mode).", "INFO")

    def _clear_teleop_anchor(self):
        """清空增量锚点与最近主臂读数（开启 / 关闭遥操作时调用；锚点在接管期间固定不变）。"""
        self.teleop_master_ref = None
        self.teleop_slave_ref = None
        self.teleop_master_gripper_ref = None
        self.teleop_slave_gripper_ref = None
        self.teleop_master_now = None
        self.teleop_master_gripper_now = None

    def _refresh_teleop_target(self):
        """本拍遥操作 target 刷新（``step()`` 调用；未开启 / 读不到 / 读取异常 → 保持原 target）。

        ``absolute`` 直连主臂读数；``delta`` 走锚点增量——锚点尚未采样时**本拍先采样**
        （增量恒 0，target = 从臂当前值），保证接管瞬间不突变。主臂有两段读数（关节 + 夹爪），
        两段同拍取、同套映射处理。

        主臂**读不到**（``None``）或**读取异常**（SDK / 控制器报错）都只是本拍不刷新：
        计数 + 限流告警（``teleop_read_failures`` / ``_note_teleop_read_failure``），
        **不置 ``last_error``**——否则一次 CAN 抖动就会经 ``step()`` 把 health 打成不健康。
        """
        if not self.teleop_enabled:
            return
        try:
            master = self._get_teleop_target()
            master_gripper = self._get_teleop_gripper()
        except Exception as exc:  # noqa: BLE001 主臂读取异常（SDK / 控制器）：本拍保持原 target，下一拍重试
            self._note_teleop_read_failure(exc)
            return
        if master is None or master_gripper is None:
            self._note_teleop_read_failure("master reading unavailable (None)")
            return  # 主臂某段读不到（尚未连接 / 读取失败）：保持原 target，不突变（下一拍重试）
        self._teleop_read_error = None  # 恢复正常：下次失败重新告警一条
        master = np.asarray(master, dtype=np.float64)
        master_gripper = np.asarray(master_gripper, dtype=np.float64)
        # 记下最近一次成功读数：采集侧「帧头跳过」用它判断主臂是否已有效移动（与 target 同源）
        self.teleop_master_now = master
        self.teleop_master_gripper_now = master_gripper
        if self.teleop_mode != self.TELEOP_MODE_DELTA:
            self.set_target_action(master)
            self.set_target_gripper(master_gripper)
            return
        if self.teleop_master_ref is None:
            self._capture_teleop_anchor(master, master_gripper)
        self.set_target_action_delta(master - self.teleop_master_ref)
        self.set_target_gripper_delta(master_gripper - self.teleop_master_gripper_ref)

    def _note_teleop_read_failure(self, reason) -> None:
        """主臂读数不可用（``None`` / 异常）：计数 + **同一原因只告警一条**（30Hz 不刷屏）。

        接管中读不到主臂是**预期状态**（操作员还没接上 / 瞬时抖动 / 主臂未使能），因此既不置
        ``last_error``、也不影响 health；但必须可观测，否则操作员只看到「遥操作开着、机械臂不动」。
        计数在 ``teleop_read_failures``（累计，恢复后不清零；恢复时下一类原因重新告警一条）。
        """
        self.teleop_read_failures += 1
        text = str(reason)
        if text != self._teleop_read_error:
            self._teleop_read_error = text
            debug_print(
                self.name,
                f"teleop master read unavailable: {text}（本拍保持原 target，下一拍重试）",
                "WARNING",
            )

    def teleop_master_sample(self) -> tuple[np.ndarray, np.ndarray] | None:
        """最近一次**成功**的主臂读数 ``(关节, 夹爪)``；未开启遥操作 / 还没读到 → ``None``。

        与 ``_get_teleop_target()`` 同源（控制线程每拍刷新），供采集侧「帧头跳过」复用，
        不需要观测线程额外读一次主臂。返回的数组**只读使用**，调用方不得就地修改。
        """
        if not self.teleop_enabled or self.teleop_master_now is None or self.teleop_master_gripper_now is None:
            return None
        return self.teleop_master_now, self.teleop_master_gripper_now

    def _get_teleop_target(self) -> np.ndarray | None:
        """遥操作**关节段**目标源（主臂 → 从臂；返回主臂关节角读数）；默认 None（无遥操作源）。

        子类实现「读主臂」这一件事即可：``absolute`` / ``delta`` 的差异全部由本基类映射。
        """
        return None

    def _get_teleop_gripper(self) -> np.ndarray | None:
        """遥操作**夹爪段**目标源（主臂夹爪读数，每臂 1 维）；默认 None（无遥操作源）。"""
        return None

    # ---- 每帧推进 --------------------------------------------------------------------
    def step(self):
        """每拍：刷新遥操作 target（若开启）→ 关节段限速前进、夹爪段直接跟随，然后下发。

        **底层始终是关节控制**（``pose`` 早已在落 target 时解算成关节）：本方法只碰
        ``action / target_action`` 这一条 ``[关节段 | 夹爪段]`` 向量，下发走 ``set_joint``。
        关节段按 ``step_rad`` 插值（防跳变）；夹爪段直接跟随目标（不插值，原行为）。
        """
        self._refresh_teleop_target()
        if self.target_action is None:
            return
        if self.action is None:
            # 以实际状态初始化（避免开始时跳变）：关节段 + 夹爪段同拍取
            self.action = np.concatenate([self.get_observation_qpos(), self.get_observation_gripper()])
        self.action[: self.QPOS] = self._step_toward(
            self.action[: self.QPOS], self.target_action[: self.QPOS], self.step_rad
        )
        self.action[self.QPOS :] = self.target_action[self.QPOS :]  # 夹爪直接跟随（不插值）
        self._apply_action(self.action)

    def _apply_action(self, action: np.ndarray):
        """把底层目标向量 ``[关节段 | 夹爪段]`` 下发到硬件（控制线程每拍；限速已在 ``step()`` 完成）
        ——**子类实现**。

        典型实现：按自己的臂接线拆出每臂关节段（``action[i*每臂关节数 : ...]``）调
        ``controller.set_joint(关节)``（限位在控制器里），夹爪段（``action[QPOS + i]``）调
        ``controller.set_gripper(夹爪)``。
        """
        raise NotImplementedError

    # ---- 每拍状态采样 / 观测组装（**原始数据，无契约键**；由 robot server 组装 standard_obs）----
    def sample_qpos(self) -> dict:
        """采样机械臂侧状态（关节角 + 夹爪 + 实测位姿 + 目标位姿 + 关节段目标）并缓存进 ``motion_state``。

        **由 env 控制线程每拍调用**（本线程是控制器唯一写者 / 读者）；seq 自增，供 server 上报。
        〚observations/qpos〛**始终是关节角**（与动作空间无关：数采 / VLA 要的就是它）；夹爪独立成键
        （``observations/gripper``）；位姿由**同一拍关节角**正解（``observations/pose``，
        ``POSE = 0`` 的机器人不提供）；**目标位姿**（``observations/pose_target`` = ``FK(关节段目标)``）
        与实测位姿同拍同源，增量动作（``pose_delta``）因此可通过观测看到解算结果。

        帧时刻不由本方法写入：观测的 ``KEY_TIMESTAMP`` 由观测线程在 ``build_observation()``
        打点（观测拍时刻），故本缓存只有状态、没有时间戳。
        """
        self.seq += 1
        qpos = self.get_observation_qpos()  # 关节角（每臂 6）
        gripper = self.get_observation_gripper()
        action = self.get_action()
        if action is None:
            action = qpos  # 无指令时以当前 qpos 作为 action（保证观测含有效 action）
        state = {self.KEY_QPOS: qpos, self.KEY_GRIPPER: gripper, self.KEY_ACTION: action}
        pose = self.get_observation_pose(qpos)
        if pose is not None:
            state[self.KEY_POSE] = pose
        target_pose = self.get_target_pose()
        if target_pose is not None:
            state[self.KEY_POSE_TARGET] = target_pose
        self.motion_state = state
        return self.motion_state

    def capture_images(self) -> dict:
        """读取各相机帧并组装为契约键（``observations/images/<cam_name>``）——观测线程调用。

        子类实现 ``get_observation_images()``（返回顺序对齐 IMAGE_NAMES 的帧列表）。
        """
        # 相机成员：observations/images/<cam_name>（顺序对齐 IMAGE_NAMES）
        return {
            f"{self.CAMERA_PREFIX}{name}": img for name, img in zip(self.IMAGE_NAMES, self.get_observation_images())
        }

    def build_observation(self) -> dict | None:
        """组装完整观测 = 最新缓存机械臂状态 + 本拍相机帧（**观测线程调用**）。

        控制线程尚未采到第一拍（启动瞬间）或机械臂读取持续失败时 ``motion_state`` 为空，
        此时返回 None（本拍不出观测，由 env 观测线程跳过，下一拍重试）。
        """
        state = self.motion_state
        if state is None:
            return None
        timestamp = time.time()  # 观测拍时刻（取帧前打点；取帧 / 落盘耗时不计入）
        return {**state, **self.capture_images(), self.KEY_TIMESTAMP: timestamp}

    def get_observation(self) -> dict:
        """完整观测 = 现场采样机械臂状态 + 相机帧（**单线程调用**）。

        ⚠️ 相机取帧会阻塞，故**勿在控制线程调用**（会拖慢机械臂步进）；env 双线程序列下
        控制线程用 ``sample_qpos()``、观测线程用 ``build_observation()``，
        本方法供单线程脚本 / 调试整体取一帧。
        """
        state = self.sample_qpos()
        timestamp = time.time()  # 帧时刻（取帧前打点，与 build_observation 同口径）
        return {**state, **self.capture_images(), self.KEY_TIMESTAMP: timestamp}

    def get_observation_qpos(self) -> np.ndarray:
        """当前帧**关节角**（扁平 ``QPOS`` 维，每臂 6）——**子类实现**。

        观测中的 ``observations/qpos`` **始终是关节角**（与当前动作空间无关）。
        读不到可信读数时应**响亮报错**（``RuntimeError``），而不是返回半个观测。
        """
        raise NotImplementedError

    def get_observation_pose(self, qpos: np.ndarray | None = None) -> np.ndarray | None:
        """末端位姿（扁平 ``POSE`` 维：各臂 ``xyz + rpy``）——**子类按需实现**，缺省不提供。

        提供位姿的机器人（``POSE > 0``）用**同一拍关节角**让控制器的静态正解函数正算
        （与位姿目标的解算同一模型）；关节读数缺失 / 非法时可返回 NaN 向量
        （下游防护会丢弃该帧位姿，比“保留上一帧旧值”诚实）。
        不提供位姿的机器人（``POSE = 0``）**不用覆盖**：此处返回 ``None``（观测里没有
        ``observations/pose`` 键）。
        """
        return None

    def get_target_pose(self) -> np.ndarray | None:
        """**目标位姿** = ``FK(关节段目标)``（扁平 ``POSE`` 维）——**子类按需实现**，缺省不提供。

        与 ``get_observation_pose()`` 用**同一套静态正解**，只是喂进去的是 ``_target_joints()``
        （关节段目标）而不是实测关节——即「底层位姿目标」。与 ``observations/pose`` 同系可比：
        「目标 − 实测」就是当前稳态误差（底层 MIT 无重力前馈时不为 0）。
        增量动作（``pose_delta``）的基准与解算结果都靠它，上位判到位也不必自己攒基准。
        不提供位姿的机器人（``POSE = 0``）**不用覆盖**：返回 ``None``（观测里没有该键）。
        """
        return None

    def get_observation_gripper(self) -> np.ndarray:
        """当前帧夹爪（扁平 ``GRIPPER`` 维，每臂 1）——**子类实现**。

        夹爪是独立动作空间，故观测也独立成键；读不到时可返回 NaN 向量（下游防护会丢弃）。
        """
        raise NotImplementedError

    def get_observation_images(self) -> list:
        """读取各相机 raw RGB 帧（list，顺序对齐 IMAGE_NAMES；子类实现）。

        由 ``capture_images()`` 组装为 observations/images/<cam_name> 成员（观测线程调用）。
        """
        raise NotImplementedError

    def get_action(self) -> np.ndarray | None:
        """当前**关节段目标**（底层实际控制量，始终关节语义）；未下发过 → None。

        注意：``pose`` 空间下这里给的是**解算后的关节目标**（底层就是关节控制），不是位姿目标；
        位姿目标由调用方自己保存（或读 ``observations/qpos`` 的当前位姿）。
        """
        if self.target_action is None:
            return None
        return self.target_action[: self.QPOS].copy()

    def get_gripper_action(self) -> np.ndarray | None:
        """当前**夹爪段目标**；未下发过 → None。"""
        if self.target_action is None:
            return None
        return self.target_action[self.QPOS :].copy()

    # ---- 生命周期（env / server 调用）----------------------------------------------------
    def connect(self):
        """连接硬件（子类实现；虚拟机器人直接 ready）。"""
        self.ready = True

    def disconnect(self):
        """断开硬件 / 释放资源（子类实现）。"""
        pass

    # ---- 通用限位插值 ------------------------------------------------------------------
    @staticmethod
    def _step_toward(current: np.ndarray, target: np.ndarray, max_step: float) -> np.ndarray:
        """限位插值：每步最多向目标靠近 max_step，防止关节数据跳变。"""
        current = np.asarray(current, dtype=np.float64)
        target = np.asarray(target, dtype=np.float64)
        delta = target - current
        step = np.clip(delta, -max_step, max_step)
        return np.where(np.abs(delta) <= max_step, target, current + step)
