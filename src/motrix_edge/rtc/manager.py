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

"""RTCManager —— 策略无关的实时动作块管理器（三元切分 + 时序平滑 + 预取 + 步号推进）。

设计见 wiki/design/motrix_edge_rtc.md。策略只提供**原始动作块**（``policy.infer_chunk``）；
本管理器负责：

  - **块队列**：``{绝对步号: 动作}``（消费过的步弹出）；``_index`` 单调递增（reset 归零）；
  - **块长上限 H**：一次推理只取策略块的前 ``action_horizon`` 步（如 10 步 = 10Hz × 1s）；
  - **三元切分**：把这块按 ``prefix_len``（前置段 P，**推理期间已被执行** → 跳过）/ 执行段（E）/
    ``suffix_len``（后缀 S，过渡到下一块）切开；默认 ``E = H - P - S``；
  - **时序平滑**：块重叠步（同一绝对步号）按 ``aggregate_fn`` 融合——新块执行段与前一块后缀
    重叠的部分加权平均，未重叠部分直接执行；
  - **预取时机**：队列剩余 ``<= prefix_len + suffix_len``（= 执行段还剩 P 步）时拉下一块——
    推理耗时的 P 步正好吃掉**执行段的尾巴**，响应回来时后缀段还没被消费 → 新块执行段与
    后缀段完整重叠融合；
  - **运行期参数**：``configure``（``infer rtc set`` / ``POST /v1/infers/rtc``）；``status`` 上报。

关键关系：``P + E + S = H``（三段把一块切开）、``E > P``（执行段要长于推理耗时，否则每步都触发
推理）、重叠步数 = ``min(S, E)``。``enabled=False`` → 无 RTC 退化：每步请求一次、只取块首步
（无块缓存 / 无平滑 / 无跳过）。
"""

from __future__ import annotations

import time

import numpy as np

from motrix_edge.rtc.base import as_action_chunk, get_aggregate_fn

# 代码缺省参数（edge.yml ``policy.rtc`` 段可覆盖；运行期 ``infer rtc set`` 可改）。
DEFAULT_RTC_CONFIG = {
    "enabled": True,  # 关闭 → 每步一次推理只取块首步（无块缓存 / 无平滑）
    "action_horizon": 50,  # 块长上限 H：一次推理只取策略块的前 H 步（<=0 = 用整块）
    "prefix_len": 0,  # 前置段 P：推理期间已被执行的前 P 步（跳过）——也是提前发请求的提前量
    "execution_horizon": None,  # 执行段 E；None = 实际块长 - P - S
    "suffix_len": 10,  # 后缀段 S（= 与下一块重叠窗口）；0 = 关闭平滑
    "aggregate_fn": "weighted_average",  # 重叠聚合：weighted_average/latest_only/average/conservative
}

_RTC_INT_KEYS = ("action_horizon", "prefix_len", "execution_horizon", "suffix_len")


def validate_params(params: dict) -> dict:
    """校验 RTC 参数（可部分）→ 规范化 ``dict``；未知键 / 非法值 → ``ValueError``。

    供 ``infer rtc set`` 命令处理器（回执 rejected）与 ``RTCManager.configure`` 共用。
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
    """实时动作块管理器：策略（``infer_chunk``）+ 参数 + 运行状态。

    生命周期由推理会话持有：进入会话构造（``build_rtc``），``reset()`` 随会话复位，
    ``infer(observation)`` 驱动每步动作（会话主循环调用），``status()`` 供 server 上报。
    """

    def __init__(self, policy, config: dict | None = None, control_hz: float | None = None):
        self._policy = policy
        self._config = dict(DEFAULT_RTC_CONFIG)
        # 控制频率（Hz）：仅用于把实测推理耗时折算成步数**上报**（供人工设定前置段 P）
        self._control_hz = float(control_hz) if control_hz else None
        self._aggregate = get_aggregate_fn(self._config["aggregate_fn"])
        # 运行状态
        self._index = 0  # 下一个待下发步号（单调；reset 归零）
        self._queue: dict[int, np.ndarray] = {}  # 绝对步号 -> 动作（已聚合）
        self._last_chunk: dict | None = None  # 最近一块的切分 / 块长（status 上报）
        self._fetches = 0  # 拉块次数（会话内累计）
        self._last_delay = 0.0  # 最近一次实测推理耗时（秒；供调 P 参考，不参与切分）
        if config:
            self.configure(**{k: v for k, v in config.items() if k in DEFAULT_RTC_CONFIG})

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
        不满足交叉约束（``P + E + S <= H``、``E > P``）→ ``ValueError``（调用方回执 rejected 400）。
        """
        merged = {**self._config, **validate_params(params)}
        validate_config(merged)
        self._config = merged
        self._aggregate = get_aggregate_fn(self._config["aggregate_fn"])
        return self.params

    def _execution_horizon(self, height: int) -> int:
        """执行段 E：显式配置优先，否则 ``实际块长 - P - S``（至少 1）；仅用于切分上报。"""
        configured = self._config["execution_horizon"]
        if configured is not None:
            return int(configured)
        return max(1, int(height) - int(self._config["prefix_len"]) - int(self._config["suffix_len"]))

    def _prefix_len(self, chunk) -> int:
        """本块要跳过的前置段 P：**手工配置**的 P 与「块起点已落后当前步号的差」取大（后者不可避免）。

        P 之所以要人工设定：推理本身耗时，返回时块首步对应的时刻已经过去（那几步机器人已经
        执行过）——如果照下发，机械臂会往回走一小段；实际耗时可用 ``status().last_delay_steps``
        （实测推理耗时折算的控制步数）作为定 P 的参考。
        """
        configured = int(self._config["prefix_len"])
        return max(configured, max(0, self._index - chunk.start_index))

    # ---- 生命周期 ------------------------------------------------------------
    def reset(self) -> None:
        """清空块队列并步号归零（策略连接不变）。"""
        self._index = 0
        self._queue = {}
        self._last_chunk = None
        self._fetches = 0
        self._last_delay = 0.0

    # ---- 步进（会话消费）------------------------------------------------------
    def infer(self, observation) -> np.ndarray | None:
        """取本步应下发的动作：必要时拉新块 → 切分 → 跳过前置段 → 入队聚合 → 弹出当前步动作。

        - ``remaining <= prefix_len + suffix_len``（执行段还剩 P 步）→ **同步拉下一块**：推理耗时的
          P 步正好吃掉执行段尾巴，后缀段完整保留给下一块做重叠融合（预取不断流）；
        - 拉块失败 / 空（``None``）→ 返回 ``None``（会话跳过本步，不升级为任务错误）；
        - ``enabled=False`` → 退化：每步请求一次、只取块首步。
        """
        if not self.enabled:
            return self._infer_direct(observation)
        remaining = self._remaining()
        trigger = int(self._config["prefix_len"]) + int(self._config["suffix_len"])
        if remaining == 0 or remaining <= trigger:
            if not self._fetch(observation):
                return None
        action = self._queue.pop(self._index, None)
        if action is None:
            return None  # 队列仍无当前步（块起始步号落后）→ 跳过
        self._index += 1
        return action

    def _infer_direct(self, observation) -> np.ndarray | None:
        """无 RTC 退化模式：请求一次、只执行块首步（不做块缓存 / 平滑 / 跳过）。"""
        chunk = as_action_chunk(self._policy.infer_chunk(observation, index=self._index), start_index=self._index)
        if chunk is None or chunk.height == 0:
            return None
        self._last_chunk = {
            "start_index": chunk.start_index,
            "height": chunk.height,
            "lens": {"prefix": 0, "execution": 1, "suffix": 0},
        }
        self._fetches += 1
        action = chunk.actions[0]
        self._index += 1
        return action

    def _fetch(self, observation) -> bool:
        """拉取并落一块：**取前 H 步** → 跳过前置段 P → 执行段 + 后缀段入队（重叠步融合）。

        一次推理只取策略块的前 ``action_horizon``（H）步（如 H=10、控制 10Hz → 1s 预测），
        再按绝对步号切三段：

          - ``prefix``（前置段 P）：推理期间机器人已经执行过，跳过——否则会往回走一小段；
          - 执行段（E）：与前一块后缀重叠的部分加权平均，未重叠部分直接执行；
          - ``suffix``（后缀段 S）：留在队列，与下一块执行段重叠融合（时序平滑）。

        跳过后同步推进 ``_index``（步号 = 物理时刻，不再落后）并丢弃队列中已过期步，
        保证本轮立即从**未过期**的块首步继续下发（不断流）。
        """
        started = time.monotonic()
        chunk = as_action_chunk(self._policy.infer_chunk(observation, index=self._index), start_index=self._index)
        self._last_delay = time.monotonic() - started
        if chunk is None:
            return False
        chunk = chunk.head(int(self._config["action_horizon"]))  # 块长上限 H
        prefix_len = self._prefix_len(chunk)
        execution_len = self._execution_horizon(chunk.height)
        suffix_len = int(self._config["suffix_len"])
        split = chunk.slice(prefix_len, execution_len, suffix_len)
        # 步号对齐物理时刻：跳过前置段（已被执行过的步），丢弃过期队列项
        self._index = max(self._index, chunk.start_index + prefix_len)
        self._queue = {index: action for index, action in self._queue.items() if index >= self._index}
        # 入队：执行段 + 后缀段；与上一块后缀重叠的步按 aggregate_fn 融合（时序平滑）
        for index, action in chunk.steps():
            if index < self._index:
                continue  # 前置段：已被执行过，跳过
            old = self._queue.get(index)
            self._queue[index] = action if old is None else self._aggregate(old, action)
        self._last_chunk = {
            "start_index": chunk.start_index,
            "height": chunk.height,
            "lens": split.lens,
        }
        self._fetches += 1
        return True

    def _remaining(self) -> int:
        """队列中从 ``_index`` 起连续未消费的步数（空为 0）。"""
        if not self._queue:
            return 0
        highest = max(self._queue)
        if highest < self._index:
            return 0
        return highest - self._index + 1

    # ---- 状态上报 ------------------------------------------------------------
    def status(self) -> dict:
        """运行状态（server ``/v1/infers`` 的 ``rtc`` 字段）：参数 + 步号 + 剩余 + 最近切分 + 实测耗时。"""
        return {
            "enabled": self.enabled,
            "params": self.params,
            "index": self._index,
            "remaining": self._remaining(),
            "fetches": self._fetches,
            "last_chunk": self._last_chunk,
            "last_delay": round(self._last_delay, 4),  # 最近一次推理耗时（秒）
            # 实测耗时折算的控制步数（定前置段 P 的参考；无 control_hz → None）
            "last_delay_steps": (
                int(round(self._last_delay * self._control_hz)) if self._control_hz and self._last_delay else 0
            ),
        }
