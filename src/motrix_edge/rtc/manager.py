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

"""RTCManager —— 策略无关的实时动作块管理器（异步预取 + 三元切分 + 时序平滑 + 步号推进）。

设计见 wiki/design/motrix_edge_rtc.md。策略只提供**原始动作块**（``policy.infer_chunk``）；
本管理器负责：

  - **块队列**：``{绝对步号: 动作}``（消费过的步弹出）；``_index`` 单调递增（reset 归零）；
  - **块长上限 H**：一次推理只取策略块的前 ``action_horizon`` 步（如 10 步 = 10Hz × 1s）；
  - **三元切分**：把这块按 ``prefix_len``（前置段 P，**推理期间已被执行** → 跳过）/ 执行段（E）/
    ``suffix_len``（后缀 S，过渡到下一块）切开；默认 ``E = H - P - S``；
  - **时序平滑**：块重叠步（同一绝对步号）按 ``aggregate_fn`` 过渡策略融合——新块执行段与前一块
    后缀重叠的部分按**下一段权重曲线**加权（本段 1→0 / 下一段 0→1），未重叠部分直接执行；
  - **异步预取**：队列剩余 ``<= prefix_len + suffix_len``（= 执行段还剩 P 步，即预取提前量
    ``P + S`` 用尽）时**在后台线程**拉下一块——控制环不阻塞，推理耗时期间由队列里的后缀段继续
    供电；响应回来时按**当时**的步号切分，推理期间已经走过的步自动落进 ``prefix``（真实过期步
    由 ``_index`` 与块首步之差算出，不必人工精确设定 P）；
  - **运行期参数**：``configure``；``status`` 上报。**命令 / HTTP 入口（``infer rtc set`` /
    ``POST /v1/infers/rtc``）与 ``status().rtc`` 字段随接线提交（#8）落地**，本包只提供参数校验与
    运行状态。

关键关系：``P + E + S = H``（三段把一块切开）、``P + S < H``、``E > P``（执行段要长于推理耗时，
否则每步都触发推理）、稳态重叠步数 ≈ ``min(S, E)``（真实值取决于响应到达时刻，上报为
``status().last_chunk.overlap_steps``）。**H 只是上限**：策略返回的块比 H 短时（openpi 16 步 vs
H=50），P / E / S 与预取提前量按 ``实际块长 / H`` 等比缩放（见 ``_segments``），否则提前量会大于
块长、变成每一拍都在推理。``enabled=False`` → 无 RTC 退化：每步请求一次、只取块首步
（无块缓存 / 无平滑，但仍跳过过期步）。

线程模型：``infer()`` 内部加锁（``enabled=False`` 退化路径没有工作线程，但簿记同样持锁、策略调用同样
在锁外）；预取在**工作线程**里调用 ``policy.infer_chunk``（策略客户端需可跨线程调用，**同一时刻至多
一个请求**）。单飞用两态表达（见 ``_claim_locked``）：``_job`` = 已登记、待执行
的请求（``reset()`` / ``close()`` 可以直接作废，它从没到过策略端）；``_running`` = 策略端正在被调用
（只能由执行者结清，唯一释放点在 ``_infer_and_apply`` 的 ``finally``）。**前提**：策略客户端需可跨线程
调用，且每次调用都要**带超时**——单飞槽的释放取决于调用返回，没有超时就没有上界（接线时落实，见 #8）。
工作线程为 daemon，**由会话退出时的 ``close()`` 回收**。
"""

from __future__ import annotations

import threading
import time

import numpy as np

from motrix_edge.rtc.base import DEFAULT_AGGREGATE_FN, as_action_chunk, get_aggregate_fn, split_lens

# 代码缺省参数（edge.yml ``policy.rtc`` 段可覆盖；运行期可经 ``configure`` 改，命令入口见 #8）。
DEFAULT_RTC_CONFIG = {
    "enabled": True,  # 关闭 → 每步一次推理只取块首步（无块缓存 / 无平滑）
    "action_horizon": 50,  # 块长上限 H：一次推理只取策略块的前 H 步（>= 1；实际取 min(H, 块长)）
    "prefix_len": 0,  # 前置段 P：额外强制跳过的前 P 步（人工安全余量；真实过期步自动跳过）
    "execution_horizon": None,  # 执行段 E；None = 实际块长 - P - S
    "suffix_len": 10,  # 后缀段 S（= 与下一块重叠窗口，也是预取提前量的一部分）；0 = 关闭平滑
    "aggregate_fn": DEFAULT_AGGREGATE_FN,  # 过渡策略（下一段权重曲线）
}

# 预取工作线程名（测试据此观测 ``close()`` 是否回收线程）
PREFETCH_THREAD_NAME = "rtc-prefetch"
# ``status().last_error`` 长度上限：状态经 GET /v1/infers 被反复轮询，异常文本不做无界外泄
_MAX_ERROR_LEN = 200

_RTC_INT_KEYS = ("action_horizon", "prefix_len", "execution_horizon", "suffix_len")


def _snapshot(observation):
    """观测的**防御性快照**（发起预取时执行）。

    观测里的数组常是共享内存视图（进程按控制频率覆写），而异步预取会在**工作线程**里读它——
    不拷贝就可能读到撕裂 / 被覆写后的值。``ndarray`` 逐层拷贝，``bytes`` / 标量等不可变对象
    原样透传。
    """
    if isinstance(observation, np.ndarray):
        return observation.copy()
    if isinstance(observation, dict):
        return {key: _snapshot(value) for key, value in observation.items()}
    if isinstance(observation, (list, tuple)):
        return type(observation)(_snapshot(value) for value in observation)
    return observation


def validate_params(params: dict) -> dict:
    """校验 RTC 参数（可部分）→ 规范化 ``dict``；未知键 / 非法值 → ``ValueError``。

    供 ``infer rtc set`` 命令处理器（接线见 #8；回执 rejected）与 ``RTCManager.configure`` 共用。
    """
    if not isinstance(params, dict):
        raise ValueError("rtc params must be a JSON object")
    unknown = [k for k in params if k not in DEFAULT_RTC_CONFIG]
    if unknown:
        raise ValueError(f"unknown rtc param(s): {unknown} (available: {list(DEFAULT_RTC_CONFIG)})")
    out: dict = {}
    if "enabled" in params:
        out["enabled"] = bool(params["enabled"])
    if "aggregate_fn" in params:
        name = str(params["aggregate_fn"])
        get_aggregate_fn(name)  # 未注册 → ValueError
        out["aggregate_fn"] = name
    for key in _RTC_INT_KEYS:
        if key not in params:
            continue
        raw = params[key]
        if raw is None:  # execution_horizon 允许显式 None（= 由 H - P - S 推导）
            if key != "execution_horizon":
                raise ValueError(f"rtc param {key} must be an integer")
            out[key] = None
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"rtc param {key} must be an integer, got {raw!r}") from None
        if key == "action_horizon" and value < 1:
            raise ValueError("rtc action_horizon must be >= 1")
        if key == "execution_horizon" and value < 1:
            raise ValueError("rtc execution_horizon must be >= 1")
        if key in ("prefix_len", "suffix_len") and value < 0:
            raise ValueError(f"rtc {key} must be >= 0")
        out[key] = value
    return out


def validate_config(config: dict) -> dict:
    """校验**合并后**的完整 RTC 参数（交叉约束）→ 规范化 ``dict``；非法 → ``ValueError``。

    约束（见 wiki/design/motrix_edge_rtc.md）：

    - ``P + E + S = H``（三段就是把这一块切开；H 是块长上限、E 缺省推导）；
    - ``P + S < H``（块内必须有可执行段），``P + E + S <= H``（E 显式设置时）；
    - ``E > P``（执行段要长于前置段，否则新块响应当步就满足预取条件 → 每步都推理）。
    """
    horizon = int(config["action_horizon"])
    prefix_len = int(config["prefix_len"])
    suffix_len = int(config["suffix_len"])
    execution = config["execution_horizon"]
    if prefix_len + suffix_len >= horizon:
        raise ValueError(
            f"rtc prefix_len + suffix_len must be < action_horizon (P={prefix_len} + S={suffix_len} >= H={horizon})"
        )
    if execution is not None and prefix_len + int(execution) + suffix_len > horizon:
        raise ValueError(
            f"rtc prefix_len + execution_horizon + suffix_len must be <= action_horizon "
            f"(P={prefix_len} + E={int(execution)} + S={suffix_len} > H={horizon})"
        )
    derived_e = horizon - prefix_len - suffix_len if execution is None else int(execution)
    if derived_e <= prefix_len:
        raise ValueError(
            f"rtc execution_horizon must be > prefix_len (E={derived_e} <= P={prefix_len}): "
            "执行段要长于推理耗时，否则每步都会触发推理"
        )
    return dict(config)


class RTCManager:
    """实时动作块管理器：策略（``infer_chunk``）+ 参数 + 运行状态 + 预取工作线程。

    生命周期由推理会话持有：进入会话构造（``build_rtc``），``infer(observation)`` 驱动每步动作
    （会话主循环调用，``async`` 下**不阻塞**），``reset()`` 随会话复位，``status()`` 供 server
    上报，**会话退出调用 ``close()``** 回收预取工作线程。
    """

    def __init__(self, policy, config: dict | None = None, control_hz: float | None = None):
        self._policy = policy
        self._config = dict(DEFAULT_RTC_CONFIG)
        # 控制频率（Hz）：仅用于把实测推理耗时折算成步数**上报**（供人工调参参考）
        self._control_hz = float(control_hz) if control_hz else None
        self._transition_fn = get_aggregate_fn(self._config["aggregate_fn"])  # 下一段权重曲线 fn(pos, length)
        # 运行状态（控制环 infer() / 工作线程落块 / server 线程 status() 并发访问 → 全部由 _lock 保护；
        # 策略调用一律在锁外，避免阻塞 status()）
        self._lock = threading.RLock()
        self._index = 0  # 下一个待下发步号（单调；reset 归零）
        self._queue: dict[int, np.ndarray] = {}  # 绝对步号 -> 动作（已聚合）
        self._trigger_steps: int | None = None  # 本次生效的预取提前量 P + S（短块按块长等比缩放）
        self._last_chunk: dict | None = None  # 最近一块的切分 / 块长（status 上报）
        self._fetches = 0  # 已发起的预取请求数（会话内累计）
        self._stale_chunks = 0  # 整块落在过去（策略滞后 / 时间戳错）被丢弃的块数
        self._failed_chunks = 0  # 策略未返回动作 / 抛异常的块数
        self._last_delay = 0.0  # 最近一次实测推理耗时（秒；供调 P 参考，不参与切分）
        self._last_error: str | None = None  # 最近一次策略异常（异步路径不外抛，经 status 暴露）
        self._calibrated_len: int | None = None  # 已据以校准 H 的实测块长（None = 尚未校准，见 calibrate）
        # 异步预取：单飞 = 「待执行的登记」+「策略端正在被调用」两态（见 _claim_locked）
        self._job: tuple | None = None  # 已登记、待执行的请求 （观测快照, 步号, 世代）
        self._running = False  # 策略端正在被调用（互斥；只有执行者能结清，见 _infer_and_apply）
        self._generation = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if config:
            self.configure(**config)  # 未知键 / 非法值 → ValueError（配置键名写错不再静默忽略）

    # ---- 参数 ----------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self._config["enabled"])

    @property
    def params(self) -> dict:
        """当前参数（``execution_horizon=None`` = 由 ``H - P - S`` 推导）。"""
        return dict(self._config)

    def configure(self, **params) -> dict:
        """运行期更新参数（校验后生效，下一块起用新参数）；返回更新后的参数。

        先逐键校验（``validate_params``），再把新旧合并后做交叉约束校验（``validate_config``）；
        不满足交叉约束（``P + S < H``、``E > P``）→ ``ValueError``（调用方回执 rejected 400）。
        加锁：运行期由会话 / server 线程调用，可能和工作线程的落块并发。
        """
        with self._lock:
            merged = {**self._config, **validate_params(params)}
            validate_config(merged)
            self._config = merged
            self._transition_fn = get_aggregate_fn(self._config["aggregate_fn"])
            return self.params

    def _scaled_segments(self, height: int) -> tuple[int, int, int]:
        """把配置的 ``(P, S)`` 按 ``height / H`` 等比缩放到块长 ``height``，并补足执行段 E。

        ``P + E + S = height``、``P + S < height``（``S`` 配置非 0 时保底 1 步，避免过渡被静默
        关掉）；``height = H``（常态）时与配置完全一致。
        """
        horizon = int(self._config["action_horizon"])
        prefix = int(self._config["prefix_len"])
        suffix = int(self._config["suffix_len"])
        scale = int(height) / horizon if horizon else 1.0
        scaled_suffix = max(1, round(suffix * scale)) if suffix > 0 else 0
        scaled_suffix = min(scaled_suffix, max(0, int(height) - 1))
        scaled_prefix = min(round(prefix * scale), max(0, int(height) - 1 - scaled_suffix))
        return scaled_prefix, int(height) - scaled_prefix - scaled_suffix, scaled_suffix

    def _segments(self, height: int) -> tuple[int, int, int]:
        """本次块的三段 ``(P, E, S)``：配置装得进**实际块长**就用配置，装不下则按 ``块长 / H`` 等比缩放。

        ``H`` 只是上限（``head`` 已把块截到 ``min(H, 实际块长)``）：策略返回的块比 H 短时（openpi 的
        16 步 vs H=50），配置的绝对步数放不进这一块——照搬会让预取提前量 ``P + S`` 大于块长 →
        **每一拍都在推理**，且上报与实际重叠步数对不上。故按比例缩放（见 ``_scaled_segments``）。
        """
        prefix = int(self._config["prefix_len"])
        suffix = int(self._config["suffix_len"])
        configured_e = self._config["execution_horizon"]
        execution = (int(height) - prefix - suffix) if configured_e is None else int(configured_e)
        if execution >= 1 and prefix + execution + suffix <= int(height):
            return prefix, execution, suffix
        return self._scaled_segments(height)

    def calibrate(self, block_len: int | None) -> dict:
        """按**实测块长**把块长上限 H 收敛为 ``min(H, block_len)``（``P/E/S`` 等比缩放）；返回参数。

        服务端不一定声明块长：openpi 官方 metadata **没有** action_horizon（``policy.action_horizon``
        缺省 50 只是猜测），lerobot-act 的 ``actions_per_chunk`` 也只是请求值。H 大于真实块长时
        三段划分没有意义（永远填不满，预取提前量 ``P + S`` 相对真实块长偏大、上报的切分与实测
        对不上）——按实测收敛后，``P/E/S`` 的相对结构与推理提前量一次性对齐真实块长，不必每块
        靠 ``_segments`` 兜底缩放。

        规则（均**不改参数**、不抛异常——校准只是兜底，不该让推理失败）：

        - ``block_len`` 缺省 / 非法（None / < 1）→ 按未观测到处理；
        - 实测 ``>=`` 当前 H → 保持（H 是我们要的执行窗口上限，实测更长不需要动）；
        - **只校准一次**（``observed_chunk_len`` 是「最近一次」实测，可能被瞬时短块污染；
          已校准 / 已显式 ``configure`` 过就不再跟随）；
        - 缩放结果不满足交叉约束（``E > P`` 等）→ 保持原参数。

        校准后 ``execution_horizon`` 回到缺省（= ``H - P - S`` 推导）：不把缩放后的绝对步数固化，
        否则后续只改 H（``infer rtc set``）会与固化的 E 冲突而被校验拒绝。

        实测块长取 ``policy.observed_chunk_len``（客户端每次拿到块回填，见
        ``BasePolicyClient._note_chunk_len``）；``infer()`` 每步按需调用（幂等）。
        """
        try:
            length = int(block_len) if block_len is not None else 0
        except (TypeError, ValueError):
            return self.params
        with self._lock:
            params = self.params
            horizon = int(params["action_horizon"])
            if self._calibrated_len is not None or length < 1 or length >= horizon:
                return params
            prefix, _execution, suffix = self._scaled_segments(length)
            # 取整可能让「填满整块」的 E 反而不大于 P（小整数）：压低前缀步保证 E > P
            prefix = min(prefix, max(0, (length - suffix - 1) // 2))
            self._calibrated_len = length  # 先记：即使下方校验不过也不再反复尝试
            try:
                return self.configure(
                    action_horizon=length, prefix_len=prefix, suffix_len=suffix, execution_horizon=None
                )
            except ValueError:  # 缩放结果非法（理论上已被上面的压低覆盖）→ 保持原参数
                return params

    def _age(self, chunk) -> int:
        """当前步相对块首步已「迟到」几步（``>= 0``）；``>= chunk.height`` = 整块落在过去。

        异步预取时随控制环推进自然增长，是**真实**过期步数（无需人工设定 P）。
        """
        return max(0, self._index - chunk.start_index)

    def _drop_stale_locked(self, chunk) -> bool:
        """整块是否已落在当前步号之前 → 计 ``stale_chunks`` 并返回 True（调用方不再下发）。

        基于 ``_age``：块尾都在过去 = 这一步都用不上（策略滞后 / 时间戳错）。**持锁调用**。
        """
        if self._age(chunk) < chunk.height:
            return False
        self._stale_chunks += 1
        return True

    def _record_failure_locked(self, exc: Exception) -> None:
        """记一次失败（策略异常 / 落块意外）：计数 + 限长错误文本。**持锁调用**。"""
        self._failed_chunks += 1
        self._last_error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]

    # ---- 生命周期 ------------------------------------------------------------
    def reset(self) -> None:
        """清空块队列、作废在途预取并步号归零（策略连接与工作线程不变）。

        作废方式是**丢掉「已登记、还没被取走」的请求**：它从没到过策略端，扔掉不涉及任何占用。
        **已经开始的那次调用不受影响**——它的占用由自己结清（``_infer_and_apply`` 的 ``finally``），
        带回的结果按世代丢弃。所以 reset 后既不会并发叠出第二个请求，也不会把单飞槽漏掉。
        """
        with self._lock:
            self._generation += 1  # 在途结果世代作废（落地时按世代丢弃）
            self._job = None  # 待执行的登记直接作废
            self._index = 0
            self._queue = {}
            self._trigger_steps = None
            self._last_chunk = None
            self._fetches = 0
            self._stale_chunks = 0
            self._failed_chunks = 0
            self._last_delay = 0.0
            self._last_error = None

    def close(self, timeout: float = 1.0) -> None:
        """停止预取工作线程（幂等；会话退出时调用）。

        在途结果随世代作废；关闭后再 ``infer()`` 不再启动新线程，预取退化为在调用线程内联完成。
        工作线程为 daemon，未调 ``close()`` 时最坏情况是运行期结束后残留一个等待事件的线程
        （不阻塞进程退出）。
        """
        with self._lock:
            self._generation += 1
            self._job = None  # 没被取走的登记直接作废（正在进行的调用由执行者结清）
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _start_worker_locked(self) -> None:
        """惰性启动预取工作线程（已在运行 → 不重复启动）。"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, name=PREFETCH_THREAD_NAME, daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        """预取工作线程：取登记 → 推理 → 按世代落块；策略异常不杀线程（经 ``status`` 报警）。

        ``_wake`` 同时承担「有新登记」与「收到停止信号」：仅在 ``close()`` 或新登记时被唤醒。
        取走登记与置位 ``_running`` 在**同一临界区**完成，中间不留「既不待执行也不在运行」的空档；
        **本轮出任何意外都只记一次失败、线程继续跑**（``_running`` 由 ``_infer_and_apply`` 的 ``finally``
        结清，不会漏掉单飞槽）。
        """
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                job, self._job = self._job, None
                if job is not None:
                    self._running = True
            if job is not None:
                try:
                    self._infer_and_apply(job, propagate=False)
                except Exception as exc:  # noqa: BLE001 —— 预取线程必须活下来（落块意外也不能杀线程）
                    with self._lock:
                        self._record_failure_locked(exc)

    # ---- 步进（会话消费）------------------------------------------------------
    def infer(self, observation) -> np.ndarray | None:
        """取本步应下发的动作：必要时发起预取（异步 / 内联）→ 弹出当前步动作。

        - 队列里有当前步 → 直接下发；同时若剩余 ``<= prefix_len + suffix_len``（预取提前量
          ``P + S`` 用尽）→ 把下一块预取交给工作线程（控制环不阻塞）；
        - 队列里**没有**当前步（首块 / 断流 / 步号跳空）→ 本步必须等结果：无在途请求时在调用
          线程**内联**取一块并取用，已有在途请求时返回 ``None``；
        - 观测在发起预取时做防御性快照（见 ``_snapshot``）：调用方之后可以立即复用 / 覆写原 buffer；
        - 队列仍无当前步 → 返回 ``None``（会话跳过本步，不升级为任务错误）；
        - ``enabled=False`` → 退化：每步请求一次、只取块首步。
        """
        # 兜底校准块长上限 H：策略不一定声明块长（openpi 官方 metadata 无 action_horizon），
        # 按**首次实测块长**收敛（一次，幂等；见 ``calibrate``）——放在最前，让首个 rollout
        # 就用校准后的 H / P / E / S，而不是先跑一块短块的降级切分。
        self.calibrate(getattr(self._policy, "observed_chunk_len", None))
        if not self.enabled:
            return self._infer_direct(observation)
        inline = None
        with self._lock:
            if self._index not in self._queue or self._remaining() <= self._trigger():
                inline = self._claim_locked(observation)
            if inline is None:  # 已交给工作线程 / 有在途请求 → 直接取当前步
                return self._pop_locked()
        self._infer_and_apply(inline, propagate=True)  # 内联：**锁外**先落块，再下发当前步
        with self._lock:
            return self._pop_locked()

    def _pop_locked(self) -> np.ndarray | None:
        """弹出当前步动作并推进步号（无动作 → ``None``，会话跳过本步）。"""
        action = self._queue.pop(self._index, None)
        if action is not None:
            self._index += 1
        return action

    def _trigger(self) -> int:
        """预取提前量：``P + S``（= 执行段还剩 P 步时开始拉下一块）；取**本次生效**的三段（短块已缩放）。"""
        if self._trigger_steps is not None:
            return self._trigger_steps
        return int(self._config["prefix_len"]) + int(self._config["suffix_len"])

    def _claim_locked(self, observation) -> tuple | None:
        """登记一次预取（登记即计入 ``fetches``）；返回需**在调用线程内联执行**的请求，否则 ``None``。

        **单飞**由两态共同表达：``_job``（已登记、待执行）与 ``_running``（策略端正在被调用），任一
        非空都不再发起——避免请求堆积 / 策略端排队。队列里还有当前步 → 放进 ``_job`` 交给工作线程
        并返回 ``None``；本步本来就没动作可下发（首块 / 断流）→ 返回 ``(快照, 步号, 世代)`` 并**就地
        占用策略端**（``_running = True``），由调用线程在锁外内联执行（否则本步必然拿不到动作）。
        """
        if self._job is not None or self._running:
            return None
        self._fetches += 1
        job = (_snapshot(observation), self._index, self._generation)  # 防御性快照（工作线程要跨线程读）
        if self._index in self._queue and not self._stop.is_set():
            self._job = job
            self._start_worker_locked()
            self._wake.set()
            return None
        self._running = True  # 内联：调用线程马上要去调用策略端
        return job

    def _infer_and_apply(self, job: tuple, *, propagate: bool) -> None:
        """执行一次已登记的预取并按**当时**的步号落块（工作线程或调用线程）。

        ``job`` = ``(观测快照, 发起时的绝对步号, 世代)``；推理期间走过的步在落块时由 ``_age``
        自动计入 ``prefix``——响应越晚，跳过的前置步越多。

        策略异常：``propagate=False``（工作线程）不外抛，记入 ``status().last_error``，否则照旧
        抛给调用方（与会话回执语义一致）。

        **世代校验覆盖全部写入**：结果（``_apply_locked``）、实测耗时与失败计数都只在世代未变时记账；
        旧世代（``reset()`` / ``close()`` 之后迟到的响应）**只结清单飞槽**——否则旧会话的超时 / 异常会
        污染新会话的 ``last_error`` / ``failed_chunks``（而 reset 正是「策略出问题后重开会话」的时刻，
        这几个字段恰恰最常被看）。``_running`` 的唯一释放点在本方法的 ``finally``。
        """
        try:
            observation, step, generation = job
            started = time.monotonic()
            try:
                chunk = as_action_chunk(self._policy.infer_chunk(observation, index=step), start_index=step)
            except Exception as exc:  # noqa: BLE001 —— 工作线程必须活下来
                with self._lock:
                    if generation == self._generation:  # 旧世代：不计数、不写 last_error
                        self._record_failure_locked(exc)
                if propagate:
                    raise
                return
            delay = time.monotonic() - started
            with self._lock:
                if generation == self._generation:  # 旧世代：耗时与结果都不计入新会话
                    self._last_delay = delay
                    self._apply_locked(chunk)
        finally:
            with self._lock:
                self._running = False

    def _apply_locked(self, chunk) -> None:
        """把一块按当前步号切分入队（重叠步融合）；整块落在过去 → 丢弃并计数。

        一次推理只取策略块的前 ``action_horizon``（H）步（如 H=10、控制 10Hz → 1s 预测），
        再按绝对步号处理三段：

          - ``prefix``（前置段 P）：推理期间已经执行过的步，跳过——否则会往回走一小段；
          - 执行段（E）：与前一块后缀重叠的步按过渡策略加权（本段 1→0 / 下一段 0→1）；
          - ``suffix``（后缀段 S）：留在队列，与下一块执行段重叠融合（时序平滑）。
        """
        if chunk is None or chunk.height == 0:
            self._failed_chunks += 1  # 策略未返回动作 / 返回空块
            return
        chunk = chunk.head(int(self._config["action_horizon"]))  # 块长上限 H → 本次实际块长 = min(H, 块长)
        plan_prefix, execution_len, suffix_len = self._segments(chunk.height)
        self._trigger_steps = plan_prefix + suffix_len  # 预取提前量用**本次缩放后**的 P + S
        # 实际跳过 = 本次的 P 与「块首步已落后当前步号」取大（真实过期步不可省）
        prefix_len = max(plan_prefix, self._age(chunk))
        lens = split_lens(chunk.height, prefix_len, execution_len, suffix_len)
        # 步号对齐物理时刻：跳过前置段（已被执行过的步），丢弃过期队列项
        self._index = max(self._index, chunk.start_index + prefix_len)
        self._queue = {step: action for step, action in self._queue.items() if step >= self._index}
        self._last_chunk = {
            "start_index": chunk.start_index,
            "height": chunk.height,
            "lens": lens,
            "overlap_steps": 0,
        }
        if self._drop_stale_locked(chunk):
            return
        # 待入队步 = 本块中绝对步号 >= 当前步号的步（等价于「块尾仍在本步之后」）
        pending = {
            chunk.start_index + i: chunk.actions[i] for i in range(chunk.height) if chunk.start_index + i >= self._index
        }
        overlap = sorted(set(pending) & set(self._queue))  # 重叠窗口（本块与下一块都覆盖的绝对步号）
        for pos, step in enumerate(overlap):
            # alpha = 下一段（新块）在该步的权重：固定搭配为常数，continuous 按步号 0→1 递增
            alpha = min(1.0, max(0.0, float(self._transition_fn(pos, len(overlap)))))
            self._queue[step] = self._queue[step] * (1.0 - alpha) + pending.pop(step) * alpha
        self._queue.update(pending)  # 未重叠步：下一块独占，直接入队
        self._last_chunk["overlap_steps"] = len(overlap)  # 与上一块重叠（参与过渡融合）的步数

    def _infer_direct(self, observation) -> np.ndarray | None:
        """无 RTC 退化模式：请求一次、只执行块首步（不做块缓存 / 过渡）。

        与 RTC 路径共用 ``_age`` 口径**跳过已过期步**：块首步落在当前步号之前时取块内对应步，
        整块过期则返回 ``None``（不拿过期动作去驱动真机）。同样先按 ``action_horizon``（H）**截断**——
        否则模型给出比 H 更长的块时，退化模式会把第 ``H`` 步之后的"深视界"动作也当可用（且 ``lens`` /
        块长上报与 RTC 路径不一致）；``head`` 顺带拷贝一份，返回值不会共享策略复用的 buffer。

        **锁的边界与 RTC 路径一致**：策略调用在锁外（``enabled=False`` 时没有工作线程，但 ``status()``
        仍会来读，把调用圈进锁会阻塞它），而**步号 / 计数 / 上报等簿记在锁内**（``_drop_stale_locked``
        的契约即「持锁调用」）；调用期间若被 ``reset()`` / ``close()`` 作废（世代变了）→ 本轮结果与
        统计都不采用。
        """
        with self._lock:
            generation, step = self._generation, self._index
        started = time.monotonic()
        chunk = as_action_chunk(self._policy.infer_chunk(observation, index=step), start_index=step)
        if chunk is not None:
            chunk = chunk.head(int(self._config["action_horizon"]))  # 块长上限 H：与 RTC 路径同口径
        delay = time.monotonic() - started
        with self._lock:
            if generation != self._generation:  # 调用期间被 reset / close 作废 → 不污染新会话
                return None
            self._last_delay = delay
            self._fetches += 1
            if chunk is None or chunk.height == 0:
                self._failed_chunks += 1
                return None
            if self._drop_stale_locked(chunk):
                return None
            age = self._age(chunk)  # 当前步在块内的下标（= 已过期步数）
            self._last_chunk = {
                "start_index": chunk.start_index,
                "height": chunk.height,
                "lens": split_lens(chunk.height, age, 1, 0),
                "overlap_steps": 0,
            }
            action = chunk.actions[age]
            self._index += 1
            return action

    def _remaining(self) -> int:
        """队列中从 ``_index`` 起**连续可用**的步数（空 / 断档 → 0）；**仅在持有 ``_lock`` 时调用**。"""
        steps = 0
        while self._index + steps in self._queue:
            steps += 1
        return steps

    # ---- 状态上报 ------------------------------------------------------------
    def status(self) -> dict:
        """运行状态（接线后经 server ``/v1/infers`` 的 ``rtc`` 字段上报，见 #8）：参数 + 步号 + 预取计数 + 最近一块。

        只有四个字段是「预取健康度」的新增上报：``inflight``（是否有请求在途）、``fetches``
        （已发起请求数）、``stale_chunks``（整块过期被丢弃 = 策略滞后）、``failed_chunks``
        （策略没数据 / 抛异常）+ ``last_error``（原因）——它们区分「某一步无动作可下发」的成因。
        """
        with self._lock:
            return {
                "enabled": self.enabled,
                "params": self.params,
                "index": self._index,
                "remaining": self._remaining(),
                "inflight": self._job is not None or self._running,  # 待执行登记或正在调用策略端
                "fetches": self._fetches,
                "stale_chunks": self._stale_chunks,
                "failed_chunks": self._failed_chunks,
                "last_chunk": self._last_chunk,
                "last_delay": round(self._last_delay, 4),  # 最近一次推理耗时（秒）
                # 实测耗时折算的控制步数（定前置段 P 的参考；无 control_hz → 0）
                "last_delay_steps": (
                    int(round(self._last_delay * self._control_hz)) if self._control_hz and self._last_delay else 0
                ),
                "last_error": self._last_error,
            }
