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
  - **三元切分**：新块按当前步号切 ``prefix``（过去已失效，丢弃）/ ``execution``（实际执行段，
    ``execution_horizon`` 步）/ ``suffix``（过渡后缀，留在队列）；
  - **时序平滑**：块重叠步（同一绝对步号）按 ``aggregate_fn`` 融合，块边界由此平滑衔接；
  - **预取时机**：队列剩余 ``<= suffix_len`` 时**同步**拉下一块，保证不断流；
  - **运行期参数**：``configure``（``infer rtc set`` / ``POST /v1/infers/rtc``）；``status`` 上报。

``enabled=False`` → 无 RTC 退化：每步请求一次、只取块首步（无块缓存 / 无平滑）。
"""

from __future__ import annotations

import numpy as np

from motrix_edge.rtc.base import as_action_chunk, get_aggregate_fn

# 代码缺省参数（edge.yml ``policy.rtc`` 段可覆盖；运行期 ``infer rtc set`` 可改）。
DEFAULT_RTC_CONFIG = {
    "enabled": True,  # 关闭 → 每步一次推理只取块首步（无块缓存 / 无平滑）
    "action_horizon": 50,  # 块长 H（信息性；缺省取策略 metadata / 客户端默认）
    "execution_horizon": None,  # 实际执行段 E；None = H - suffix_len
    "suffix_len": 10,  # 过渡后缀 S（= 与下一块重叠窗口）；0 = 关闭平滑
    "inference_delay": 0,  # 前缀步数 D（信息性：预期块前部已失效的步数）
    "aggregate_fn": "weighted_average",  # 重叠聚合：weighted_average/latest_only/average/conservative
}

_RTC_INT_KEYS = ("action_horizon", "execution_horizon", "suffix_len", "inference_delay")


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
        if raw is None:  # execution_horizon 允许显式 None（= 由 H - suffix_len 推导）
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
        if key in ("suffix_len", "inference_delay") and value < 0:
            raise ValueError(f"rtc {key} must be >= 0")
        out[key] = value
    return out


class RTCManager:
    """实时动作块管理器：策略（``infer_chunk``）+ 参数 + 运行状态。

    生命周期由推理会话持有：进入会话构造（``build_rtc``），``reset()`` 随会话复位，
    ``infer(observation)`` 驱动每步动作（会话主循环调用），``status()`` 供 server 上报。
    """

    def __init__(self, policy, config: dict | None = None):
        self._policy = policy
        self._config = dict(DEFAULT_RTC_CONFIG)
        self._aggregate = get_aggregate_fn(self._config["aggregate_fn"])
        # 运行状态
        self._index = 0  # 下一个待下发步号（单调；reset 归零）
        self._queue: dict[int, np.ndarray] = {}  # 绝对步号 -> 动作（已聚合）
        self._last_chunk: dict | None = None  # 最近一块的切分 / 块长（status 上报）
        self._fetches = 0  # 拉块次数（会话内累计）
        if config:
            self.configure(**{k: v for k, v in config.items() if k in DEFAULT_RTC_CONFIG})

    # ---- 参数 ----------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self._config["enabled"])

    @property
    def params(self) -> dict:
        """当前参数（``execution_horizon=None`` = 由 ``H - suffix_len`` 推导）。"""
        return dict(self._config)

    def configure(self, **params) -> dict:
        """运行期更新参数（校验后生效，下一块起用新参数）；返回更新后的参数。"""
        self._config.update(validate_params(params))
        self._aggregate = get_aggregate_fn(self._config["aggregate_fn"])
        return self.params

    def _execution_horizon(self, height: int) -> int:
        """实际执行段步数：显式配置优先，否则 ``H - suffix_len``（至少 1）。"""
        configured = self._config["execution_horizon"]
        if configured is not None:
            return int(configured)
        return max(1, int(height) - int(self._config["suffix_len"]))

    # ---- 生命周期 ------------------------------------------------------------
    def reset(self) -> None:
        """清空块队列并步号归零（策略连接不变）。"""
        self._index = 0
        self._queue = {}
        self._last_chunk = None
        self._fetches = 0

    # ---- 步进（会话消费）------------------------------------------------------
    def infer(self, observation) -> np.ndarray | None:
        """取本步应下发的动作：必要时拉新块 → 切分 → 入队聚合 → 弹出当前步动作。

        - 队列剩余 ``<= suffix_len``（含 0）→ **同步预取**下一块（块重叠步融合 = 时序平滑）；
        - 拉块失败 / 空（``None``）→ 返回 ``None``（会话跳过本步，不升级为任务错误）；
        - ``enabled=False`` → 退化：每步请求一次、只取块首步。
        """
        if not self.enabled:
            return self._infer_direct(observation)
        remaining = self._remaining()
        if remaining == 0 or remaining <= int(self._config["suffix_len"]):
            if not self._fetch(observation):
                return None
        action = self._queue.pop(self._index, None)
        if action is None:
            return None  # 队列仍无当前步（块起始步号落后）→ 跳过
        self._index += 1
        return action

    def _infer_direct(self, observation) -> np.ndarray | None:
        """无 RTC 退化模式：请求一次、只执行块首步（不做块缓存 / 重叠）。"""
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
        """拉取并落一块（切分：prefix 丢弃 / execution+suffix 入队聚合）；无块 → False。"""
        chunk = as_action_chunk(self._policy.infer_chunk(observation, index=self._index), start_index=self._index)
        if chunk is None:
            return False
        # 三元切分（按当前步号对齐：块前部落在过去步号上 = prefix，已失效）
        prefix_len = max(0, self._index - chunk.start_index)
        execution_len = self._execution_horizon(chunk.height)
        suffix_len = int(self._config["suffix_len"])
        split = chunk.slice(prefix_len, execution_len, suffix_len)
        # 入队：丢弃 prefix；execution + suffix 入队，重叠步按 aggregate_fn 融合（时序平滑）
        for index, action in chunk.steps():
            if index < self._index:
                continue  # prefix：过去时刻，已失效
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
        """运行状态（server ``/v1/infers`` 的 ``rtc`` 字段）：参数 + 步号 + 剩余 + 最近切分。"""
        return {
            "enabled": self.enabled,
            "params": self.params,
            "index": self._index,
            "remaining": self._remaining(),
            "fetches": self._fetches,
            "last_chunk": self._last_chunk,
        }
