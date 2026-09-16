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
- **扁平动作**：一维数组，维度由 ``QPOS`` 声明（各臂关节 + 夹爪拼接）。
  例：双臂 6 关节 + 1 夹爪 → ``QPOS`` = 14，动作 ``[左6关节, 左夹爪, 右6关节, 右夹爪]``。
- **原始观测**：``sample_qpos()`` / ``build_observation()``（单线程调试可用 ``get_observation()``）
  产出 ``{observations/qpos, observations/images/<cam>, action, timestamp}``——键名复用 adapter
  契约常量（``KEY_QPOS`` / ``CAMERA_PREFIX`` / ``KEY_TIMESTAMP``）；robot server
  （contract_server）据此组装 standard_obs 并写入共享内存。
- **控制 / 观测分线程（env 双线程序列）**：控制线程每拍 ``step()`` 限速推进 +
  ``sample_qpos()`` 采样机械臂状态（qpos / action）缓存进 ``motion_state``；观测线程每拍
  ``build_observation()``（= 缓存状态 + 本拍相机帧），观测帧的 ``timestamp`` 由**观测线程**
  在打帧时写入（不是控制拍的采样时刻）。
  机械臂读取与相机取帧因此分处两线程，相机卡顿只会让观测丢帧，不会拖慢机械臂步进；
  单线程脚本仍可用 ``get_observation()`` 一次取整帧。
- **名字**：展示名取配置 ``robot.name``，缺省回退类常量 ``NAME``（机型默认名）——同型号多机在
  配置里命名区分（如 ``dual_piper_pc16``）。
- **硬件接线只在配置里**：控制器端口 / 相机设备来自 ``robot.ports`` / ``robot.cameras``（键清单
  由子类声明）；**值必填**，缺失 / 空串 → 构造即报错。代码不内置现场值，**也不虚构占位值**
  （写假序列号只会把错误推迟到 SDK「找不到设备」）。详见 ``robot-pipeline/README.md``。
- **控制**：``reset()`` / ``execute()`` / ``rollout()`` / ``safe_stop()`` 只修改
  ``target_action``，实际运动由 env 控制线程每拍 ``step()`` 限速推进（单一目标模型）。
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


class BaseRobot:
    # ---- 身份 / 能力（对齐 adapter 契约；子类覆盖）----
    NAME = "base_robot"  # 展示名缺省值（配置 robot.name 可覆盖）
    ADAPTER_TYPE = "base_robot"  # 本机器人对应的 adapter 类型（entry point 名）
    ROBOT_MODEL_ID = "base-robot"
    ROBOT_MODEL_VERSION = "0.0.0"
    CAPABILITIES: dict[str, bool] = {}

    # ---- 布局 / 特性（子类覆盖）----
    QPOS = 14  # 扁平动作维度（各臂关节 + 夹爪拼接）
    IMAGE_NAMES: list[str] = []  # 相机名（observations/images/<name>）
    IMAGES: dict[str, tuple[int, int]] = {name: (640, 480) for name in IMAGE_NAMES}  # 相机名 -> (w, h)
    SHM_NAME = "robot_obs"  # 观测共享内存名（server 侧发布）

    # ---- 观测键（adapter 契约约定，与 Edge 侧 / robot server 保持一致；勿改）----
    KEY_QPOS = "observations/qpos"  # 关节 qpos 键
    KEY_ACTION = "action"  # 进程侧当前目标动作（非 qpos 副本，共享内存单独一段）
    CAMERA_PREFIX = "observations/images/"  # 相机图像键前缀（<prefix><cam_name>）
    KEY_TIMESTAMP = "timestamp"  # 观测帧时刻（观测线程打帧时写入，非控制拍采样时刻）

    # ---- 遥操作模式（取值与 /v1/teleop 的 mode 同名；子类一般不用覆盖）----
    TELEOP_MODE_ABSOLUTE = "absolute"  # 主臂绝对位姿直连从臂 target（示教采集）
    TELEOP_MODE_DELTA = "delta"  # 锚点增量：target = slave_ref + (master_now − master_ref)（人工接管）
    TELEOP_MODES = (TELEOP_MODE_ABSOLUTE, TELEOP_MODE_DELTA)

    def __init__(self, robot_config: dict | None = None):
        self.robot_config = dict(robot_config or {})
        # 展示名：配置 ``robot.name`` 覆盖，缺省回退类常量 ``NAME``（机型默认名）
        self.name = str(self.robot_config.get("name") or self.NAME).strip()
        # 每帧最大关节增量（rad）：限速插值步长，可经配置 step_rad 修改
        self.step_rad = float(self.robot_config.get("step_rad", 0.1))
        self.ready = False
        self.last_error = None

        # 控制状态：action 当前执行位置；target_action 目标（reset/execute/rollout 都只覆盖它）
        self.action: np.ndarray | None = None
        self.target_action: np.ndarray | None = None
        # 复位目标：默认全零（子类可按配置 init_qpos 覆盖）
        self.init_qpos = self.robot_config.get("init_qpos")
        if self.init_qpos is None:
            raise ValueError(f"Robot {self.name} init_qpos is not set in robot_config.")

        # 遥操作：开关 + 映射模式 + 增量锚点（锚点在接管后首拍采样，见 _capture_teleop_anchor）
        self.teleop_enabled = False
        self.teleop_mode = self.TELEOP_MODE_ABSOLUTE
        self.teleop_master_ref: np.ndarray | None = None  # 主臂锚点读数（delta 模式）
        self.teleop_slave_ref: np.ndarray | None = None  # 从臂锚点位姿（delta 模式，接管瞬间）

        # 控制拍计数（env 控制线程每拍 ``sample_qpos()`` 自增；server 组装 standard_obs 时附带）
        # ——与观测发布频率（``OBS_HZ``）解耦，不是观测帧号
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
    @classmethod
    def action_dim(cls) -> int:
        return cls.QPOS  # 扁平动作维度（各臂关节 + 夹爪拼接）

    # ---- 控制：HTTP（reset/execute/rollout/safe_stop）都只修改 target_action -----------
    def reset(self):
        """程序复位到 home（非阻塞）：target_action = init_qpos（env 每帧限速接近）。"""
        self.disable_teleop()
        self.set_target_action(self.init_qpos)

    def execute(self, action: np.ndarray):
        """直接下发动作指令（raw）：扁平动作 → target_action。"""
        self.disable_teleop()
        self.set_target_action(action)

    def rollout(self, action: np.ndarray) -> bool:
        """推理闭环：**遥操作（人工接管）进行中则拒绝**（返回 False）。

        遥操作开启即视为人工接管（不区分 ``absolute`` / ``delta``）：从臂 target 由人工决定，
        推理下发必须让位——本方法**不改 target、也不退出遥操作**（与 ``execute()`` 的「程控抢回」
        相反），遥操作关闭（``disable_teleop()``）后自动恢复。
        非遥操作状态下与 ``execute()`` 一致（只修改唯一 target_action）。
        """
        if self.teleop_enabled:
            debug_print(self.name, "rollout refused: teleop (human takeover) active.", "WARNING")
            return False
        self.disable_teleop()
        self.execute(action)
        return True

    def safe_stop(self):
        """安全停止（幂等、失败安全）：退出遥操作并清空目标，step() 不再推进。

        **本接口是「软停」**：停止下发新目标并保持当前位姿（关节仍带力矩），**不断电**，
        因此机械臂不会失力下垂。硬件急停（断电 e-stop，`PiperController.emergency_stop`）
        **不在这条路径上**——由现场急停按钮 / 作业流程负责；待实现「掉力后受控阻尼下坠」
        流程后再评估是否接入，届时本契约需同步更新。
        """
        self.disable_teleop()  # 退出遥操作（含清空增量锚点）
        self.target_action = None
        debug_print(self.name, "Safe stop executed.", "WARNING")

    def set_target_action(self, target_action: np.ndarray | None):
        """设置目标动作（绝对值）：step() 每帧把 action 朝 target_action 限速移动。

        增量（人工接管）写入 target 请用 ``set_target_action_delta()``（锚点 + 增量）。
        """
        self.target_action = None if target_action is None else np.asarray(target_action, dtype=np.float64)

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
        """在**增量锚点**（接管瞬间的从臂位姿）上叠加增量，设置 target_action。

        ``target_action = teleop_slave_ref + delta``——增量遥操作写 target 的唯一入口
        （``delta`` = 主臂读数 − 主臂锚点；关节与夹爪同一套语义，逐元素相加）。
        ``step_rad`` 限速仍由 ``step()`` 完成，本接口**不做任何限幅**（增量只决定 target，
        不限制幅度）。锚点未采样时返回 False 且不写 target（本拍保持原目标，不突变）。
        """
        if self.teleop_slave_ref is None:
            return False
        self.set_target_action(self.teleop_slave_ref + np.asarray(delta, dtype=np.float64))
        return True

    def current_action(self) -> np.ndarray:
        """从臂当前位姿参考（``action`` → ``target_action`` → ``init_qpos``）。

        供增量接管采样从臂锚点：接管后 target 从该位姿出发，此前残留（可能已发散）的
        推理 target 被丢弃，从臂动作因此连续。
        """
        if self.action is not None:
            return self.action.copy()
        if self.target_action is not None:
            return self.target_action.copy()
        return np.asarray(self.init_qpos, dtype=np.float64)

    def _capture_teleop_anchor(self, master_now: np.ndarray):
        """采样增量接管锚点：主臂读数 + 从臂当前位姿（**同一拍**，即接管瞬间）。

        此后 ``target = slave_ref + (master_now − master_ref)``：增量恒从 0 开始，主从位姿差
        被一次性零化（人工把主臂摆到与从臂相近位姿后接管，二者叠加后动作连续）。
        """
        self.teleop_master_ref = np.asarray(master_now, dtype=np.float64)
        self.teleop_slave_ref = self.current_action()
        debug_print(self.name, "Teleop anchor captured (delta mode).", "INFO")

    def _clear_teleop_anchor(self):
        """清空增量锚点（开启 / 关闭遥操作时调用；锚点在接管期间固定不变）。"""
        self.teleop_master_ref = None
        self.teleop_slave_ref = None

    def _refresh_teleop_target(self):
        """本拍遥操作 target 刷新（``step()`` 调用；未开启 / 主臂读不到 → 保持原 target）。

        ``absolute`` 直连主臂读数；``delta`` 走锚点增量——锚点尚未采样时**本拍先采样**
        （增量恒 0，target = 从臂当前位姿），保证接管瞬间不突变。
        """
        if not self.teleop_enabled:
            return
        master_now = self._get_teleop_target()
        if master_now is None:
            return  # 主臂读取失败 / 未连接：保持原 target，不突变（下一拍重试）
        master_now = np.asarray(master_now, dtype=np.float64)
        if self.teleop_mode != self.TELEOP_MODE_DELTA:
            self.set_target_action(master_now)
            return
        if self.teleop_master_ref is None:
            self._capture_teleop_anchor(master_now)
        self.set_target_action_delta(master_now - self.teleop_master_ref)

    def _get_teleop_target(self) -> np.ndarray | None:
        """遥操作目标源（主臂 → 从臂；返回主臂**绝对**扁平读数）；默认 None（无遥操作源）。

        子类实现「读主臂」这一件事即可：``absolute`` / ``delta`` 的差异全部由本基类映射。
        """
        return None

    # ---- 每帧推进 --------------------------------------------------------------------
    def step(self):
        """每帧：刷新遥操作 target（若开启）→ 把 action 朝 target_action 限速接近并执行。"""
        self._refresh_teleop_target()
        if self.target_action is None:
            return
        if self.action is None:
            self.action = self._init_action_from_qpos()

        self.action[0:6] = self._step_toward(self.action[0:6], self.target_action[0:6], self.step_rad)
        self.action[6] = self.target_action[6]  # 夹爪直接跟随目标（不插值）
        self.action[7:13] = self._step_toward(self.action[7:13], self.target_action[7:13], self.step_rad)
        self.action[13] = self.target_action[13]  # 夹爪直接跟随目标（不插值）

        self._apply_action(self.action)

    def _init_action_from_qpos(self) -> np.ndarray:
        """以当前实际状态初始化 action（避免开始时跳变）。"""
        qpos = self.get_observation_qpos()
        return qpos

    def _apply_action(self, action: np.ndarray):
        """把 action 下发 / 应用到硬件（子类实现；虚拟机器人同步到合成状态）。"""
        raise NotImplementedError

    # ---- 每拍状态采样 / 观测组装（键名复用 adapter 契约常量，不依赖 profile；由 robot server 组装 standard_obs）----
    def sample_qpos(self) -> dict:
        """采样机械臂侧状态（qpos + action）并缓存进 ``motion_state``。

        **由 env 控制线程每拍调用**（本线程是控制器唯一写者 / 读者）；``seq`` 自增（控制拍
        计数，与观测发布频率无关），供 server 上报。
        观测组装只读本缓存，故相机 / 磁盘卡顿不会拖慢机械臂读取与步进。
        子类实现 ``get_observation_qpos()``——直接从控制器「手搓」取数据。

        帧时刻不由本方法写入：观测的 ``KEY_TIMESTAMP`` 由观测线程在 ``build_observation()``
        打点（观测拍时刻），故本缓存只有状态、没有时间戳。
        """
        self.seq += 1
        qpos = self.get_observation_qpos()
        action = self.get_action()
        if action is None:
            action = qpos  # 无指令时以当前 qpos 作为 action（保证观测含有效 action）
        self.motion_state = {self.KEY_QPOS: qpos, self.KEY_ACTION: action}
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

        帧时刻 ``KEY_TIMESTAMP`` 由**本线程**在此打点（= 观测拍时刻，取帧之前）：相机帧即本拍取，
        机械臂状态（qpos / action）来自上一个控制拍（最多早约 1 个控制周期）。

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

        帧时刻与 ``build_observation()`` **同口径**：采完机械臂状态、**取相机帧之前**打点
        （取帧阻塞不计入帧时刻）。
        """
        state = self.sample_qpos()
        timestamp = time.time()  # 帧时刻（取帧前打点，与 build_observation 同口径）
        return {**state, **self.capture_images(), self.KEY_TIMESTAMP: timestamp}

    def get_observation_qpos(self) -> np.ndarray:
        """读取当前帧原始观测的 qpos（扁平 QPOS 维；子类实现）。"""
        raise NotImplementedError

    def get_observation_images(self) -> list:
        """读取各相机 raw RGB 帧（list，顺序对齐 IMAGE_NAMES；子类实现）。

        由 ``capture_images()`` 组装为 observations/images/<cam_name> 成员（观测线程调用）。
        """
        raise NotImplementedError

    def get_action(self) -> np.ndarray | None:
        """当前执行中的 action（无则 None）。"""
        return self.action.copy() if self.action is not None else None

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
