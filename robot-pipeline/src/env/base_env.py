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

"""BaseEnv —— 机器人运行环境（server 进程内，**控制 / 观测双线程**；只控制 robot）。

职责（不碰 HTTP / 共享内存 / 契约——那些归 robot server / contract_server）：
- **控制线程**（``HZ``，默认 30Hz）：取本拍运动指令 → ``robot.step()`` 限速接近 target
  → ``robot.sample_qpos()`` 采样机械臂状态（qpos / action）缓存。
  相机取帧与采集落盘都**不在**本线程，故相机 / 磁盘卡顿不会拖慢或中断机械臂控制。
- **观测线程**（``OBS_HZ``，默认 10Hz = **真实取观测的频率**）：取相机帧 + 缓存状态拼装观测 → 帧钩子 ``on_frame``
  （server 据此读取观测并发布共享内存）→ 采集落盘。
- **命令队列**：HTTP 线程（server handler）调用 ``robot_reset`` / ``robot_execute`` /
  ``robot_rollout`` / ``robot_safe_stop`` / ``robot_set_teleop`` **只把运动指令入队**，由
  控制线程每拍取出执行；``robot_capture_*`` **只把采集指令入队**，由观测线程取出执行。
  机器人只有控制线程一个写者、collector 只有观测线程一个写者，HTTP 线程不直接碰它们，
  避免跨线程竞态。

  ⚠️ **跨队列顺序不保证**（有意）：FIFO 只在各自队列内成立；采集指令 ≤ 1/``OBS_HZ``（默认
  ≈100ms）在观测拍生效、运动指令 ≤ 1/``HZ``（默认 ≈33ms）在控制拍生效——``capture start``
  可能晚于同批 ``execute`` 生效（episode 首尾最多 ~0.1s 运动不落盘）。**当前范围只支持外部
  驱动采集**（人在 console / HTTP 点击，间隔 ≫ 100ms），env 不在控制流程内自行启动采集；
  后续若要支持「在控制流程中启动采集」，必须先解决这个同步点（见
  ``wiki/design/robot_pipeline_runtime.md``「跨队列顺序（有意弱化）」）。

  **遥操作（人工接管）期间推理让位**：``robot_rollout`` 在 HTTP 线程同步抛
  ``TakeoverActiveError``（server → 409），控制线程执行时 ``BaseRobot.rollout()`` 再判一次
  （判据都是 ``robot.teleop_enabled``，**不分遥操作模式**）；``execute`` / ``reset`` / ``safe_stop``
  不受影响（程控 / 安全优先，执行即结束遥操作）。

层次：server (HTTP + 共享内存) → env (控制 / 观测线程 + 命令队列) → robot (action/target_action/step 限速)

env 收到采集开关的请求启动采集开关，从收到采集开始，到收到采集结束，会保存其中的一段episode（mcap）
env 收到启动遥操作请求后，robot就会进入遥操作模式，robot会自行开始跟随遥操作。
"""

import queue
import threading
import time
from collections import deque

import numpy as np
from collector import get_collector
from utils.base.data_handler import debug_print


class TakeoverActiveError(RuntimeError):
    """遥操作（人工接管）进行中：推理下发（``rollout``）被拒（server 映射为 409）。"""


class BaseEnv:
    HZ = 30.0  # 运动控制频率（Hz，可修改；改小/大即调整限速节奏）
    OBS_HZ = 10.0  # 观测频率（Hz，可修改）= **真实取观测**的节奏（取帧 / 发布 / 落盘）
    OBS_SLOW_FRAME_S = 0.5  # 观测慢帧告警阈值（秒）：单拍 step_observe() 超时即告警
    # 连续 step 失败达到该次数 → 上报 loop_error（≈0.33s @30Hz）：单帧偶发失败只计数，
    # 持续失败（硬件 / 控制器已读不出来）必须降级，否则 Edge 看到 ok 会继续下发指令。
    STEP_ERROR_LIMIT = 10
    # 采集「帧头跳过」（``collector.skip_until_motion``，只对**遥操作录制**生效）：capture 开始后
    # 主臂读数相对首帧未超阈值 → 本拍不记录（连 episode 都不开），直到出现一次有效移动；
    # 此后微小位移照常记录，下一轮 episode 重新武装。阈值按现场主臂手感调（rad / 归一化）。
    HEAD_SKIP_DEFAULTS = {"enabled": True, "joint_eps": 0.05, "gripper_eps": 0.05}

    def __init__(self, robot, capture_config: dict | None = None):
        self.robot = robot
        self._running = False
        self._control_thread: threading.Thread | None = None
        self._observe_thread: threading.Thread | None = None
        self.loop_alive = False  # 控制线程是否存活（供 health 的 ready 组合）
        self.observe_alive = False  # 观测线程是否存活（供 health 的 ready 组合）
        self.last_error = None  # 最近一次命令 / 控制拍异常（health 上报；**sticky**，需重启清除）
        self.loop_error = None  # 控制线程持续失败（连续 step 异常超阈值；恢复后自动清除）
        self._step_error_streak = 0  # step 连续失败计数（任一帧成功即清零）
        self.on_frame = None  # 每帧回调（server 设置：读取观测并发布共享内存）
        self.observation = {}  # 观测线程保留的最新原始观测副本（其他消费方只读）
        # 命令队列：HTTP 线程入队，各自由唯一消费者线程取出执行（组件单写者）。
        # ⚠️ FIFO 只保证队列内顺序：两条队列生效时机不同（≤1/HZ vs ≤1/OBS_HZ），跨队列顺序不保证。
        self.commands: queue.Queue = queue.Queue()  # 运动指令（控制线程消费，≤1/HZ 生效）
        self.capture_commands: queue.Queue = queue.Queue()  # 采集指令（观测线程消费，≤1/OBS_HZ 生效）

        # 采集：capturing=True 时观测拍记录观测；False 时保存为一条 episode
        # collector 类型由 capture_config 里的 `type` 决定（当前支持 act_mcap，默认 mcap）
        # 帧头跳过（collector.skip_until_motion）：只在「遥操作录制」时武装，阈值按现场调
        self._head_skip_cfg = {
            **self.HEAD_SKIP_DEFAULTS,
            **((capture_config or {}).get("skip_until_motion") or {}),
        }
        self._head_skip: dict | None = None  # 帧头跳过状态（None = 未在跳过，正常记录）
        self.capturing = False
        self._collector = get_collector(
            capture_config or {"type": "act_mcap", "save_dir": "./data", "image_format": "jpeg"}
        )
        # 注入机器人身份：collector 写 JSON 元信息时附带 robot_name / robot_type
        set_robot_meta = getattr(self._collector, "set_robot_meta", None)
        if set_robot_meta is not None:
            set_robot_meta(
                robot_name=str(self.robot.name),
                robot_type=str(getattr(self.robot, "ADAPTER_TYPE", "")),
            )
        self._episode_open = False  # 当前是否有未关闭的 episode

        # 实测帧率统计：两线程各自最近周期窗口（帧间隔秒），health / 慢帧告警倒推实测 Hz。
        # 窗口由各自线程追加、由 HTTP 线程（health）读取：不额外加锁——纯统计量，最坏读到
        # 相邻两拍混算的平均值（差 < 1 拍），不影响展示与告警判定。
        self._control_periods: deque[float] = deque(maxlen=30)
        self._control_prev_t = 0.0
        self._observe_periods: deque[float] = deque(maxlen=30)
        self._observe_prev_t = 0.0
        self._slow_observe_warn_t = 0.0  # 观测慢帧告警上次输出时刻（限流，秒）

    # ---- 控制方法（HTTP 线程调用：只入队，不直接碰 robot / collector；由消费者线程执行）---------
    def _check_action_dim(self, flat_action, action_space=None):
        """同步校验动作维度 + 动作空间（HTTP 线程即时反馈，不触碰 robot 状态）。

        两种空间的**扁平维度相同**（每臂 7 维），差别在语义，所以除了维度还要校验空间是否
        被机器人声明（``ACTION_SPACES``）——未声明 → ``ValueError``（server 映射 422）。
        """
        dim = self.robot.action_dim(self.robot.normalize_action_space(action_space))
        if len(flat_action) != dim:
            raise ValueError(
                f"action dim {len(flat_action)} != {self.robot.normalize_action_space(action_space)} dim {dim}"
            )
        if action_space is not None:
            self.robot.normalize_action_space(action_space)

    def robot_reset(self):
        """程序复位到 home（非阻塞）：入队，由控制线程执行。"""
        debug_print(self.robot.name, "HTTP 收到命令: reset（复位到 home）", "INFO")
        self.commands.put(("reset", None))

    def robot_execute(self, flat_action, action_space=None):
        """直接下发动作指令（raw）：入队，由控制线程执行（同步校验维度 / 动作空间）。

        ``action_space=pose`` 时由 robot 在控制线程**解算一次**再运行（失败只影响
        该条命令：日志 WARNING + 目标不变，见 ``_drain_commands``）。
        """
        self._check_action_dim(flat_action, action_space)
        debug_print(self.robot.name, f"HTTP 收到命令: execute dim={len(flat_action)} space={action_space}", "INFO")
        self.commands.put(("execute", (flat_action, action_space)))

    def robot_rollout(self, flat_action, action_space=None):
        """推理闭环：入队，由控制线程执行（同步校验维度 + **遥操作中同步拒绝**）。

        **遥操作（人工接管，不分模式）进行中** → 抛 ``TakeoverActiveError``（server 映射 HTTP 409）：
        从臂 target 由人工决定，推理下发必须让位；控制线程执行时 ``BaseRobot.rollout()``
        **再判一次**（权威）——两次判定之间遥操作状态可能变化，以控制线程为准。
        ``action_space`` 语义同 ``robot_execute``。
        """
        self._check_action_dim(flat_action, action_space)
        if self.robot.teleop_enabled:
            raise TakeoverActiveError("teleop (human takeover) active: rollout refused")
        debug_print(self.robot.name, f"HTTP 收到命令: rollout dim={len(flat_action)} space={action_space}", "INFO")
        self.commands.put(("rollout", (flat_action, action_space)))

    def robot_safe_stop(self):
        """安全停止（软停：停发指令并保持位姿、不断电；幂等、失败安全）：入队，由控制线程执行。"""
        debug_print(self.robot.name, "HTTP 收到命令: safe_stop（急停）", "INFO")
        self.commands.put(("safe_stop", None))

    def robot_set_teleop(self, enabled: bool, mode: str | None = None):
        """设置遥操作（``enabled``：True=遥操作 / False=程控；``mode``：映射模式）：入队，控制线程执行。

        ``mode`` 为 ``absolute``（缺省，主臂绝对位姿直连）或 ``delta``（**人工接管**：以接管
        瞬间的主 / 从位姿为锚点，只叠加主臂增量）——具体语义与锚点采样见 ``BaseRobot``。
        """
        debug_print(self.robot.name, f"HTTP 收到命令: teleop enabled={bool(enabled)} mode={mode}", "INFO")
        self.commands.put(("teleop", (bool(enabled), mode)))

    def robot_capture_start(self):
        """开始一轮采集（episode 开始）：入队，由观测线程置 capturing=True。

        ≤ 1/``OBS_HZ``（默认 ≈100ms）生效；与运动指令**无跨队列顺序保证**（见模块 docstring）。
        """
        debug_print(self.robot.name, "HTTP 收到命令: capture/start（开始采集）", "INFO")
        self.capture_commands.put(("capture_start", None))

    def robot_capture_end(self):
        """结束一轮采集（episode 结束）：入队，由观测线程置 capturing=False 并保存。

        ≤ 1/``OBS_HZ``（默认 ≈100ms）生效；与运动指令**无跨队列顺序保证**（见模块 docstring）。
        """
        debug_print(self.robot.name, "HTTP 收到命令: capture/end（结束采集）", "INFO")
        self.capture_commands.put(("capture_end", None))

    def robot_capture_sync(self, meta: dict):
        """同步采集元信息（operator / task_name / description 等）到 collector：入队。

        元信息由 adapter 的 ``capture sync``（POST /v1/capture/sync）从 console / web
        同步进来；collector 在结束一轮采集写同名 JSON 元信息时附加。
        """
        debug_print(self.robot.name, f"HTTP 收到命令: capture/sync meta={meta}", "INFO")
        self.capture_commands.put(("capture_sync", dict(meta or {})))

    def health(self) -> dict:
        """健康检查：就绪（硬件 + 两线程存活 + 无错误）+ 最近错误 + 控制频率。

        ``control_hz`` = 名义控制频率（控制线程 ``HZ``）；``measured_hz`` = 控制线程最近
        窗口实测帧率（含 step 耗时与 sleep 抖动，更能反映真实控制节奏）。相机 / 落盘
        卡顿只影响观测线程，不体现在 ``measured_hz``。

        ``loop_error`` 覆盖「控制线程在转、但硬件已持续读不出来」的降级态（连续 step
        失败超阈值）：否则这种情况只有日志，Edge 会看到 ok 并继续下发 execute / rollout。

        ``ready`` 已含**两线程存活**与无错误，故 health 不单列线程组合值（``loop_alive`` /
        ``observe_alive`` 是各自的存活位：由两个线程在启动 / 退出时置位，只供 ``ready`` 组合消费，
        不是对外的 health 字段）。
        """
        err = self.last_error or self.loop_error or self.robot.last_error
        return {
            "ready": self.robot.ready and self.loop_alive and self.observe_alive and err is None,
            "last_error": err,
            "control_hz": self.control_hz,
            "measured_hz": self.measured_hz,
        }

    @property
    def control_hz(self) -> float:
        """名义控制频率（控制线程 ``HZ``，sleep 补偿逼近）。"""
        return float(self.HZ)

    @property
    def measured_hz(self) -> float:
        """控制线程实测帧率（最近窗口平均周期倒推）；无样本时回退名义 HZ。"""
        return self._measured_hz(self._control_periods, self.HZ)

    @property
    def measured_observe_hz(self) -> float:
        """观测线程实测帧率（相机 / 落盘卡顿时低于 ``OBS_HZ``）；无样本时回退名义 OBS_HZ。"""
        return self._measured_hz(self._observe_periods, self.OBS_HZ)

    @staticmethod
    def _measured_hz(periods: deque[float], nominal_hz: float) -> float:
        """由周期窗口平均值倒推实测频率（无样本时回退名义频率）。"""
        if not periods:
            return float(nominal_hz)
        return 1.0 / (sum(periods) / len(periods))

    def capture_status(self) -> dict:
        """采集状态：运行位 + 采集元信息 + 数据目录 + 帧头跳过进度。

        元信息为 collector 的 ``meta`` 全集（含 ``capture sync`` 同步的字段，如
        ``operator`` / ``task_name``——**不另设同义顶层字段**，消费方直接读 ``meta``）；
        ``head_skip`` = 帧头跳过进度（``{"skipped": n}``；``None`` = 未在跳过）——供操作员
        区分「正在等主臂移动」与「已开始记录」。
        供 server 的 ``GET /v1/capture/status`` 上报，adapter 侧 ``capture_status()``
        消费（**合并**了原 ``data_status``：数据目录随采集状态一并上报）。
        """
        return {
            "running": self.capturing,
            "meta": getattr(self._collector, "meta", {}) or {},
            "data_dir": str(self._collector.save_dir.resolve()),
            "head_skip": None if self._head_skip is None else {"skipped": self._head_skip["skipped"]},
        }

    def observe(self) -> dict:
        """最新原始观测副本（观测线程每拍保留；只读，不推进）。

        ``robot.build_observation()`` **只在 env 观测线程中调用**（``OBS_HZ``）；共享内存
        发布等其它消费方一律读本副本，避免重复触发机器人取数。
        """
        return self.observation

    # ---- 生命周期 ----------------------------------------------------------------------
    def start(self):
        """启动：连接机器人并拉起**控制线程 + 观测线程**（server 的 lifespan 调用）。"""
        if self._running:
            return
        self.robot.connect()
        self._running = True
        self._control_thread = threading.Thread(
            target=self._control_loop, daemon=True, name=f"{self.robot.name}-control"
        )
        self._observe_thread = threading.Thread(
            target=self._observe_loop, daemon=True, name=f"{self.robot.name}-observe"
        )
        self._control_thread.start()
        self._observe_thread.start()

    def stop(self):
        """停止：退出两线程并断开机器人（共享内存写者由 server 关闭）。

        观测线程可能正阻塞在相机取帧（RealSense ``wait_for_frames`` 最长 5s），故给其
        更长的 join 超时；仍超时则留作 daemon 线程随进程退出。
        """
        self._running = False
        if self._control_thread is not None:
            self._control_thread.join(timeout=2.0)
            self._control_thread = None
        if self._observe_thread is not None:
            self._observe_thread.join(timeout=6.0)
            self._observe_thread = None
        self.robot.disconnect()

    def _control_loop(self):
        """控制线程：以 HZ 频率驱动 ``step_control()``；单次异常不退出，记录后继续。

        本线程是机械臂控制回路的唯一执行者（不碰相机 / 采集）；相机或磁盘卡顿只表现为
        观测线程丢帧（慢帧告警），机械臂仍按 HZ 稳定步进。
        """
        interval = 1.0 / self.HZ
        self.loop_alive = True
        debug_print(self.robot.name, f"env control loop started @ {self.HZ:.1f}Hz", "INFO")
        self._control_prev_t = time.perf_counter()
        try:
            while self._running:
                t0 = time.perf_counter()
                try:
                    self.step_control()
                    self._step_ok()
                except Exception as exc:  # noqa: BLE001 单帧异常不拖垮循环
                    self._step_failed(exc)
                now = time.perf_counter()
                # 帧间隔（含 step + sleep 补偿，首帧仅 step 无 sleep，窗口平均可忽略）
                self._control_periods.append(now - self._control_prev_t)
                self._control_prev_t = now
                sleep = interval - (now - t0)
                if sleep > 0:
                    time.sleep(sleep)

        finally:
            self.loop_alive = False

    def _step_ok(self):
        """step 成功：清零连续失败计数；已上报的 loop_error 随之清除（health 复位）。"""
        if self._step_error_streak == 0:
            return
        self._step_error_streak = 0
        if self.loop_error is not None:
            self.loop_error = None
            debug_print(self.robot.name, "env step recovered（health 恢复正常）", "INFO")

    def _step_failed(self, exc: Exception):
        """step 失败：计数 + 打日志（只在首次与达阈值时各一次，不刷屏）。

        达 ``STEP_ERROR_LIMIT`` 后置 ``loop_error``，由 ``/v1/health`` 上报为不健康；
        一旦有一帧成功就清零并清除，不需要人工重启。
        """
        self._step_error_streak += 1
        if self._step_error_streak in (1, self.STEP_ERROR_LIMIT):
            debug_print(self.robot.name, f"env step error ({self._step_error_streak}): {exc}", "ERROR")
        if self._step_error_streak >= self.STEP_ERROR_LIMIT:
            self.loop_error = f"env step failed {self._step_error_streak}x consecutively: {exc}"

    def _observe_loop(self):
        """观测线程：以 OBS_HZ 频率驱动 ``step_observe()``（取帧 + 发布 + 落盘）。

        相机 / 磁盘卡顿只影响本线程（丢帧 + 慢帧告警），不会拖慢控制线程的机械臂步进。
        """
        interval = 1.0 / self.OBS_HZ
        self.observe_alive = True
        debug_print(self.robot.name, f"env observe loop started @ {self.OBS_HZ:.1f}Hz", "INFO")
        self._observe_prev_t = time.perf_counter()
        try:
            while self._running:
                t0 = time.perf_counter()
                try:
                    self.step_observe()
                except Exception as exc:  # noqa: BLE001 单帧异常不拖垮循环（如相机无帧）
                    debug_print(self.robot.name, f"env observe step error: {exc}", "ERROR")
                now = time.perf_counter()
                self._observe_periods.append(now - self._observe_prev_t)
                self._observe_prev_t = now
                self._warn_slow_observe(now - t0)
                sleep = interval - (now - t0)
                if sleep > 0:
                    time.sleep(sleep)

        finally:
            self.observe_alive = False

    def _warn_slow_observe(self, elapsed: float):
        """观测慢帧告警（相机 / 落盘卡顿诊断）：超过阈值告警，最多每秒一条。"""
        if elapsed < self.OBS_SLOW_FRAME_S:
            return
        now = time.perf_counter()
        if now - self._slow_observe_warn_t < 1.0:
            return
        self._slow_observe_warn_t = now
        debug_print(
            self.robot.name,
            f"observe frame took {elapsed:.2f}s (camera / disk stall, observed {self.measured_observe_hz:.1f}Hz)",
            "WARNING",
        )

    def _drain_commands(self):
        """取出本拍所有待执行运动指令并应用到 robot（控制线程内，robot 单写者）。

        两条错误通道**语义分开**：``ValueError``（调用方给的参数 / 目标不可达，如笛卡尔 IK 失败）
        只影响该条命令——记 WARNING、目标保持不变，**不置 ``last_error``**（否则一个坏目标会把
        Edge 看到的 health 打成 not ready）；其余异常视为硬件 / 控制器故障，进 ``last_error``。
        """
        while True:
            try:
                cmd, payload = self.commands.get_nowait()
            except queue.Empty:
                break
            try:
                if cmd == "reset":
                    self.robot.reset()
                elif cmd == "execute":
                    action, action_space = payload
                    self.robot.execute(action, action_space)
                elif cmd == "rollout":
                    action, action_space = payload
                    if not self.robot.rollout(action, action_space):
                        # 遥操作中：推理让位（预期状态，只记日志，不置 last_error / 不影响 health）
                        debug_print(self.robot.name, "rollout ignored: teleop (human takeover) active.", "WARNING")
                elif cmd == "safe_stop":
                    self.robot.safe_stop()
                elif cmd == "teleop":
                    enabled, mode = payload
                    if not enabled:
                        self.robot.disable_teleop()
                    elif mode:
                        self.robot.enable_teleop(mode)  # 显式模式（delta = 人工接管）
                    else:
                        self.robot.enable_teleop()  # 缺省 absolute（兼容只发 enabled 的调用方）
                else:
                    debug_print(self.robot.name, f"unknown command '{cmd}' ignored", "WARNING")
            except ValueError as exc:
                # 命令被拒（动作空间不支持 / 笛卡尔目标不可达）：目标未更新，机械臂保持原动作
                debug_print(self.robot.name, f"command '{cmd}' rejected: {exc} (target unchanged)", "WARNING")
            except Exception as exc:  # noqa: BLE001 单条命令失败不阻断其余
                # 存字符串：health 的 detail 是 str 字段，存异常对象会在序列化时丢成 {}
                self.last_error = f"command '{cmd}' failed: {exc}"
                debug_print(self.robot.name, f"command '{cmd}' failed: {exc}", "ERROR")

    def _drain_capture_commands(self):
        """取出本拍所有待执行采集指令（观测线程内，collector 单写者）。"""
        while True:
            try:
                cmd, payload = self.capture_commands.get_nowait()
            except queue.Empty:
                break
            try:
                if cmd == "capture_start":
                    self.capturing = True
                    self._arm_head_skip()
                elif cmd == "capture_end":
                    self.capturing = False
                    self._finish_head_skip()
                elif cmd == "capture_sync":
                    self._collector.set_meta(payload)
                else:
                    debug_print(self.robot.name, f"unknown capture command '{cmd}' ignored", "WARNING")
            except Exception as exc:  # noqa: BLE001 单条命令失败不阻断其余
                self.last_error = f"capture command '{cmd}' failed: {exc}"
                debug_print(self.robot.name, f"capture command '{cmd}' failed: {exc}", "ERROR")

    def step_control(self):
        """控制拍（控制线程，HZ）：执行本拍运动指令 → 驱动机器人限速步进 → 采样机械臂状态。

        采样结果缓存进 ``robot.motion_state``，供观测线程拼装观测；本拍不读相机，
        故相机 / 磁盘卡顿不会占住控制回路。
        """
        self._drain_commands()
        self.robot.step()
        self.robot.sample_qpos()

    def step_observe(self):
        """观测拍（观测线程，OBS_HZ）：取相机帧 + 缓存状态 → 观测副本 → 帧回调 → 采集落盘。

        ``robot.build_observation()`` 只在本方法调用；帧回调（如共享内存发布）读
        ``self.observation`` 副本，不重复触发取数。控制线程尚未采到第一拍时本拍无观测。

        观测帧的 ``timestamp`` 由 ``build_observation()``（本线程）在打帧时写入，故帧时刻反映
        观测节奏（``OBS_HZ``），不是控制拍的机械臂采样时刻。
        """
        self._drain_capture_commands()
        observation = self.robot.build_observation()
        if observation is None:
            return  # 控制线程尚未采到第一拍（启动瞬间）：本拍不出观测，下一拍重试
        self.observation = observation  # 保留最新原始观测副本
        if self.on_frame is not None:
            self.on_frame(self.observation)
        self._update_capture()

    def _update_capture(self):
        """按 capturing 记录观测：True→记录本帧；False→结束并保存为一条 episode。

        ⚠️ 采集开关在同一拍**开头**（``_drain_capture_commands``）统一生效：同一拍内 drain 到
        ``capture_start`` + ``capture_end``（``capturing`` 先真后假、``_episode_open`` 从未置位）
        → **不产出任何文件**；相邻两拍各自生效则只含 1 帧。

        流式 collector（实现了 start()，如 ActMcapCollector）在 capture 开始时由观测线程
        调用 start() 打开文件（UUID 命名），之后每拍 collect 直接落盘；缓冲式 collector
        （如缓冲式 collector）无 start，仍按 collect→finish 一次性保存。

          帧头跳过（``collector.skip_until_motion``）：**遥操作录制**时先武装闸门，主臂相对
          capture 开始那一拍未出现有效移动前**不记录、也不开 episode**（文件在首帧真正要记录时
          才创建），解锁后微小位移照常记录；整轮都没动 → 不产出文件（与「同拍 start/end」一致）。
        """
        if self.capturing:
            if not self._head_skip_release():
                self._head_skip["skipped"] += 1  # 仍在等主臂有效移动（闸门打开时 _head_skip 必存在）
                return
            if not self._episode_open:
                self._episode_open = True
                start = getattr(self._collector, "start", None)
                if start is not None:
                    start()
            self._collector.collect(self.observation)
        elif self._episode_open:
            self._episode_open = False
            self._collector.finish()  # 写 footer + 同名 JSON 元信息（文件列表不经契约上报）

    def _arm_head_skip(self) -> None:
        """``capture_start``：遥操作录制时武装「帧头跳过」（否则不启用，按原行为直接记录）。

        基准 = **本拍的主臂读数**（``teleop_master_sample()``，与下发 target 同源）；主臂此刻
        读不到（未开启遥操作 / 主臂还没读到）→ 不武装：宁可按原行为记录，也不静默丢帧。
        """
        self._head_skip = None
        if not self._head_skip_cfg.get("enabled", True):
            return
        sample = self.robot.teleop_master_sample()
        if sample is None:
            return
        joints, gripper = sample
        self._head_skip = {
            "ref_joints": np.array(joints, dtype=np.float64, copy=True),
            "ref_gripper": np.array(gripper, dtype=np.float64, copy=True),
            "skipped": 0,
        }
        debug_print(
            self.robot.name,
            "capture: 帧头跳过已武装（等主臂有效移动；"
            f"关节 eps={self._head_skip_cfg['joint_eps']} / 夹爪 eps={self._head_skip_cfg['gripper_eps']}）",
            "INFO",
        )

    def _head_skip_release(self) -> bool:
        """帧头跳过：本拍是否可以开始记录（主臂有效移动 / 遥操作已关闭）→ True（并解锁）。"""
        state = self._head_skip
        if state is None:
            return True
        sample = self.robot.teleop_master_sample()
        if sample is None:
            if not self.robot.teleop_enabled:  # 不再有主臂输入 → 不再等（否则会一直不记录）
                self._release_head_skip(state, "teleop off")
                return True
            return False  # 遥操作中但主臂还没读到：继续等
        joints, gripper = sample
        moved_joint = float(np.max(np.abs(np.asarray(joints, dtype=np.float64) - state["ref_joints"])))
        moved_gripper = float(np.max(np.abs(np.asarray(gripper, dtype=np.float64) - state["ref_gripper"])))
        if moved_joint <= float(self._head_skip_cfg["joint_eps"]) and moved_gripper <= float(
            self._head_skip_cfg["gripper_eps"]
        ):
            return False
        self._release_head_skip(state, f"motion joint={moved_joint:.3f} / gripper={moved_gripper:.3f}")
        return True

    def _release_head_skip(self, state: dict, reason: str) -> None:
        """解锁闸门并记一条日志（含跳过的帧数，便于与「文件里少了多少帧」对照）。"""
        self._head_skip = None
        debug_print(
            self.robot.name,
            f"capture: 帧头跳过结束（{reason}），跳过 {state['skipped']} 帧后开始记录",
            "INFO",
        )

    def _finish_head_skip(self) -> None:
        """``capture_end``：整轮都没出现有效移动 → 本轮**不产出文件**（与「同拍 start/end」一致）。"""
        state = self._head_skip
        if state is None:
            return
        self._head_skip = None
        debug_print(
            self.robot.name,
            f"capture: 本轮未记录（主臂无有效移动，跳过 {state['skipped']} 帧）",
            "WARNING",
        )
