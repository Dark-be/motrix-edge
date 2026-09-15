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

"""rtc（实时动作块管理器）单元测试 —— fake policy（只返回原始动作块），无网络无硬件可跑。

覆盖：三元切分步数（prefix / execution / suffix）、重叠过渡（按步号的下一段权重曲线）、
异步预取（非阻塞 / 单飞 / reset 不释放单飞槽 / 过期块丢弃 / 观测快照 / 入队与策略 buffer 解耦 /
异常与非有限值不杀线程 / close 回收 / reset 作废结果）、步号推进、过期步跳过、enabled=False
退化、运行期参数校验 / 更新、状态上报、edge.yml 下发配置的合法性。

预取只跑后台线程，因此时序类用例统一用 ``_drive``：每步之后若预取在途就等它落地
（= 响应在下一步之前到达），断言随之确定。
"""

import threading
import time

import numpy as np
import pytest

from motrix_edge.config import load_config
from motrix_edge.rtc import (
    DEFAULT_RTC_CONFIG,
    ActionChunk,
    RTCManager,
    build_rtc,
    validate_config,
    validate_params,
)
from motrix_edge.rtc.base import as_action_chunk, get_aggregate_fn, split_lens
from motrix_edge.rtc.manager import PREFETCH_THREAD_NAME

DIM = 2
ASYNC_TIMEOUT = 2.0


class _FakePolicy:
    """只实现 ``infer_chunk``（策略唯一职责：取推理结果）；可变块序列，每个块一个标量值。"""

    def __init__(self, blocks, start_offset=None):
        """blocks: [[v1, v2, ...], ...] 依次返回；start_offset: 块首步相对当前步的偏移。"""
        self._blocks = [list(b) for b in blocks]
        self._start_offset = start_offset
        self.calls = 0
        self.indices: list[int | None] = []

    def infer_chunk(self, observation, index=None):
        self.calls += 1
        self.indices.append(index)
        spec = self._blocks[min(self.calls - 1, len(self._blocks) - 1)]
        start = int(index or 0) if self._start_offset is None else int(index or 0) + self._start_offset
        actions = np.array([[float(v)] * DIM for v in spec], dtype=float)
        return ActionChunk(actions=actions, start_index=start)


def _rtc(policy, **config):
    """默认 manager（异步预取；``suffix_len`` 由用例覆盖）。"""
    return build_rtc(policy, {"enabled": True, "suffix_len": 1, "aggregate_fn": "weighted_average", **config})


def _obs() -> dict:
    return {"observations/qpos": np.zeros(DIM, dtype=np.float32)}


def _wait_until(condition, timeout: float = ASYNC_TIMEOUT) -> bool:
    """轮询等待条件成立（工作线程落块是异步的）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def _drive(rtc, steps: int) -> list:
    """逐步骤进并返回动作标量；每步若有预取在途则等它落地（= 响应在下一步之前到达）。"""
    values = []
    for _ in range(steps):
        action = rtc.infer(_obs())
        values.append(None if action is None else float(action[0]))
        if rtc.status()["inflight"]:
            assert _wait_until(lambda: rtc.status()["inflight"] is False)
    return values


def _prefetch_threads() -> list:
    """当前存活的预取工作线程（close() 回收的观测点）。"""
    return [t for t in threading.enumerate() if t.name == PREFETCH_THREAD_NAME]


# ---- ActionChunk / 三元切分步数 ------------------------------------------------


def test_action_chunk_normalizes_single_step():
    """单步动作（[dim]）规范化为 [1, dim]；未声明步号时 ``start_index`` 为 None（由管理器补齐）。"""
    chunk = ActionChunk(actions=np.array([1.0, 2.0]))
    assert chunk.height == 1
    assert chunk.dim == 2
    assert chunk.start_index is None


def test_action_chunk_rejects_bad_shape():
    with pytest.raises(ValueError, match="2-D"):
        ActionChunk(actions=np.zeros((2, 2, 2)))


def test_action_chunk_rejects_non_finite_actions():
    """NaN / Inf 下发到真机 = 位置或速度失控 → 构造即拒绝（整块不可信）。"""
    with pytest.raises(ValueError, match="non-finite"):
        ActionChunk(actions=np.array([[1.0, np.nan]]))
    with pytest.raises(ValueError, match="non-finite"):
        ActionChunk(actions=np.array([[1.0], [np.inf]]))


def test_as_action_chunk_fills_missing_start_index():
    """块首步号：策略声明则以策略为准，未声明（None）/ 直接给 ndarray → 用请求步号补齐。

    否则块会被当成从 0 起，整块落进 ``prefix`` 被当过期丢弃（策略什么都下发不了且不易察觉）。
    """
    assert as_action_chunk(None) is None
    assert as_action_chunk(np.zeros((2, DIM)), start_index=7).start_index == 7
    assert as_action_chunk(ActionChunk(actions=np.zeros((2, DIM))), start_index=7).start_index == 7
    declared = ActionChunk(actions=np.zeros((2, DIM)), start_index=3)
    assert as_action_chunk(declared, start_index=7).start_index == 3  # 策略自知优先


def test_edge_yml_rtc_section_is_valid(monkeypatch):
    """包内 ``edge.yml`` 下发的 ``policy.rtc`` 必须是合法 RTC 配置（键名 + 交叉约束）。"""
    monkeypatch.delenv("MOTRIX_CONFIG_DIR", raising=False)
    config = load_config("edge.yml")["policy"]["rtc"]
    assert set(config) <= set(DEFAULT_RTC_CONFIG)  # 键名写错会在构造期 ValueError
    merged = {**DEFAULT_RTC_CONFIG, **validate_params(config)}
    assert validate_config(merged) == merged


def test_split_lens_three_way():
    """三元切分步数：prefix（过去已失效）/ execution（执行）/ suffix（过渡）。"""
    assert split_lens(10, 2, 5, 3) == {"prefix": 2, "execution": 5, "suffix": 3}


def test_split_lens_clamps_overflow():
    """步数超过块长时依次截断（execution / suffix 吃满剩余，再多余的丢弃）。"""
    assert split_lens(4, 1, 99, 99) == {"prefix": 1, "execution": 3, "suffix": 0}
    assert split_lens(6, 8, 1, 1) == {"prefix": 6, "execution": 0, "suffix": 0}
    assert split_lens(6, 0, 4, 2) == {"prefix": 0, "execution": 4, "suffix": 2}


# ---- 参数校验 -----------------------------------------------------------------


def test_validate_params_ok_and_unknown_key():
    assert validate_params({"suffix_len": 5, "enabled": False, "aggregate_fn": "latest_only"}) == {
        "suffix_len": 5,
        "enabled": False,
        "aggregate_fn": "latest_only",
    }
    assert validate_params({"aggregate_fn": "continuous"}) == {"aggregate_fn": "continuous"}
    with pytest.raises(ValueError, match="unknown rtc param"):
        validate_params({"bogus": 1})


@pytest.mark.parametrize(
    "params",
    [
        {"suffix_len": -1},
        {"prefix_len": -3},
        {"action_horizon": 0},
        {"execution_horizon": 0},
        {"aggregate_fn": "bogus"},
        {"suffix_len": "abc"},
    ],
)
def test_validate_params_rejects_invalid(params):
    with pytest.raises(ValueError):
        validate_params(params)


# ---- 步进 / 预取 / 平滑 --------------------------------------------------------


def test_rtc_executes_chunk_steps_without_refetching():
    """一个块支撑执行段多步：块内不为每步重复请求（预取只在剩余 <= P + S 时）。"""
    policy = _FakePolicy([[1, 1, 1, 1, 1, 1]])  # H=6
    rtc = _rtc(policy, suffix_len=2)  # E = H - S = 4，提前量 = 2

    assert _drive(rtc, 4) == [1.0] * 4
    assert policy.calls == 1  # 4 步只拉 1 块（剩余 2 步时下一次 infer 会预取）
    assert rtc.status()["index"] == 4
    assert policy.indices[0] == 0  # 首次以当前绝对步号请求


def test_rtc_prefetches_when_entering_suffix_window():
    """队列剩余 <= P + S 时发起下一块预取；响应按**当时**步号切分 → 与剩余后缀步重叠融合。"""
    policy = _FakePolicy([[1, 1, 1, 1], [2, 2, 2, 2]])  # H=4
    rtc = _rtc(policy, suffix_len=2)  # E = 2，提前量 = 2

    values = _drive(rtc, 4)  # 第 3 步（绝对步号 2）触发预取
    assert policy.calls == 2
    assert policy.indices == [0, 2]  # 预取时的绝对步号
    # 块 2 在绝对步号 3（= 上一块最后一步）与旧值重叠：0.3 × 旧(1) + 0.7 × 新(2)
    assert values == [1.0, 1.0, 1.0, pytest.approx(1.7)]
    assert rtc.status()["last_chunk"]["overlap_steps"] == 1


def test_rtc_temporal_smoothing_weighted_sequence():
    """时序平滑序列：每块 4 步 / S=2 → 每 E=2 步拉一块，重叠步按 0.3 本段 + 0.7 下一段融合。"""
    policy = _FakePolicy([[1] * 4, [2] * 4, [3] * 4, [4] * 4])
    rtc = _rtc(policy, suffix_len=2)  # E = 2，提前量 = 2

    assert _drive(rtc, 8) == [
        1.0,
        1.0,
        1.0,
        pytest.approx(1.7),  # 块 2 与上一块后缀重叠
        2.0,
        pytest.approx(2.7),  # 块 3 与块 2 后缀重叠
        3.0,
        pytest.approx(3.7),  # 块 4 与块 3 后缀重叠
    ]
    assert policy.calls == 4  # 预取发生在绝对步号 0 / 2 / 4 / 6
    assert policy.indices == [0, 2, 4, 6]


def test_rtc_aggregate_latest_only():
    """aggregate_fn=latest_only：重叠步直接取新块值（硬切换）。"""
    policy = _FakePolicy([[1, 1, 1, 1], [2, 2, 2, 2]])
    rtc = _rtc(policy, suffix_len=2, aggregate_fn="latest_only")

    assert _drive(rtc, 4) == [1.0, 1.0, 1.0, 2.0]


def test_rtc_aggregate_continuous_ramps_next_segment_weight():
    """aggregate_fn=continuous：重叠区按步号线性过渡——本段权重 1→0、下一段权重 0→1。"""
    policy = _FakePolicy([[1] * 6, [2] * 6])
    rtc = _rtc(policy, action_horizon=6, suffix_len=3, aggregate_fn="continuous")  # E = 3，提前量 = 3

    values = _drive(rtc, 6)
    # 重叠 2 步（绝对步号 4 / 5）：下一段权重 0 / 1 → 旧值保持 1.0、随后整步交给新块 2.0
    assert values == [1.0, 1.0, 1.0, 1.0, 1.0, 2.0]
    assert rtc.status()["last_chunk"]["overlap_steps"] == 2


def test_aggregate_functions_are_next_segment_weight_curves():
    """过渡策略表：表项是「重叠步的下一段权重曲线」——固定搭配给常数，continuous 按步号 0→1。"""
    for name, alpha in (("weighted_average", 0.7), ("conservative", 0.3), ("average", 0.5), ("latest_only", 1.0)):
        fn = get_aggregate_fn(name)
        assert fn(0, 5) == pytest.approx(alpha)
        assert fn(4, 5) == pytest.approx(alpha)  # 固定搭配：与步号无关

    continuous = get_aggregate_fn("continuous")
    assert [continuous(pos, 5) for pos in range(5)] == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])
    assert continuous(0, 1) == pytest.approx(1.0)  # 单步重叠：退化为直接采用下一段


def test_rtc_discards_prefix_actions():
    """块首步落在当前步号之前（过去时刻）→ 该 prefix 段已失效，被丢弃（仅统计）。"""
    policy = _FakePolicy([[9, 9, 9, 9, 9]], start_offset=-2)  # 块从 index-2 起
    rtc = _rtc(policy, suffix_len=1)
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}

    action = rtc.infer(obs)
    assert float(action[0]) == 9.0  # 当前步（index 0）取到块内第 3 步（prefix 已被跳过）
    status = rtc.status()
    assert status["last_chunk"]["start_index"] == -2
    assert status["last_chunk"]["lens"]["prefix"] == 2  # 前 2 步为过去时刻，已失效


# ---- 前置段跳过（P）/ 块长上限（H）-----------------------------------------


def test_rtc_skips_configured_prefix_len():
    """前置段 P：**手工设置**跳过的前 P 步（已被执行过，再下发会往回走）。"""
    policy = _FakePolicy([[float(i) for i in range(1, 21)], [float(i) for i in range(101, 121)]])
    rtc = _rtc(policy, action_horizon=20, prefix_len=2, suffix_len=4)  # E = 20 - 2 - 4 = 14
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}

    # 每步值 = 绝对步号 + 1；首块跳过前 2 步（索引 0/1 已执行过）→ 本步直接下发第 3 步
    values = [float(rtc.infer(obs)[0]) for _ in range(3)]
    assert values == [3.0, 4.0, 5.0]
    status = rtc.status()
    assert status["index"] == 5  # 步号按物理时刻推进（不滞后）
    assert status["last_chunk"]["lens"] == {"prefix": 2, "execution": 14, "suffix": 4}


def test_rtc_action_horizon_caps_chunk(monkeypatch):
    """块长上限 H：策略返回 20 步，只取前 10 步（10Hz × 1s 预测）。"""
    policy = _FakePolicy([[float(i) for i in range(1, 21)]])
    rtc = _rtc(policy, action_horizon=10, prefix_len=2, suffix_len=4)  # E = 10 - 2 - 4 = 4
    rtc.infer({"observations/qpos": np.zeros(DIM, dtype=np.float32)})
    last = rtc.status()["last_chunk"]
    assert last["height"] == 10  # 只取前 10 步
    assert last["lens"] == {"prefix": 2, "execution": 4, "suffix": 4}
    assert rtc.status()["remaining"] == 7  # 队列只剩 10 - 2 步，本步已消费 1 步


def test_rtc_execution_overlaps_previous_suffix():
    """预取提前量 = P + S：响应回来时后缀段仍在队列 → 与下一块执行段重叠加权平均。

    H=10 / P=2 / S=4（E = 4，提前量 = 6），块值 = 块号×100 + 块内下标（块 1/2/3 = 100s/200s/300s）：

    - 块 1：跳 P=2 → 队列 = 下标 2..9（102..109）；
    - 下标 4（队列剩 P+S=6 步）→ 发请求，本步仍下发 104；
    - 块 2 到达（此时已走到下标 5）：`_age = 5 - 4 = 1` → 跳 P=2 → 从下标 6 入队，与块 1 的
      后缀步（下标 6..9）重叠 → 加权 0.3 本段 + 0.7 下一块；
    - 下个请求在下标 8（块 2 的队列剩 6 步）→ 每 E = 4 步一个块。
    """
    policy = _FakePolicy([[100 + i for i in range(10)], [200 + i for i in range(10)], [300 + i for i in range(10)]])
    rtc = _rtc(policy, action_horizon=10, prefix_len=2, suffix_len=4)  # E = 4

    values = _drive(rtc, 6)
    assert values[0:3] == [102.0, 103.0, 104.0]  # 块 1 未重叠部分：直接执行
    assert values[3:5] == [pytest.approx(0.3 * 106 + 0.7 * 202), pytest.approx(0.3 * 107 + 0.7 * 203)]
    assert values[5] == pytest.approx(0.3 * 108 + 0.7 * 204)
    assert policy.indices == [0, 4, 8]  # 每次都在「队列剩 P+S = 6 步」时请求
    assert rtc.status()["last_chunk"]["lens"] == {"prefix": 2, "execution": 4, "suffix": 4}


def test_rtc_prefetch_trigger_is_prefix_plus_suffix():
    """预取触发点 = 「执行段还剩 P 步」→ ``remaining <= P + S``（不是后缀段开始）。"""
    policy = _FakePolicy([[float(i) for i in range(1, 21)], [float(i) for i in range(101, 121)]])
    rtc = _rtc(policy, action_horizon=20, prefix_len=2, suffix_len=4)  # E = 20 - 2 - 4 = 14
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}

    for _ in range(12):  # 下标 2..13：remaining 17..7 > P+S=6 → 不拉块
        rtc.infer(obs)
    assert policy.calls == 1
    rtc.infer(obs)  # 下标 14：remaining = 6 <= P+S → 执行段（下标 2..15）还剩 P=2 步时拉下一块
    assert _wait_until(lambda: policy.calls == 2)  # 预取在工作线程
    assert policy.indices == [0, 14]
    rtc.close()


def test_rtc_reports_measured_delay_steps(monkeypatch):
    """实测推理耗时折算的控制步数只作**上报**（供人工定 P）；不自动改变跳过步数。"""
    policy = _FakePolicy([[float(i) for i in range(1, 21)]])
    rtc = build_rtc(policy, {"action_horizon": 20, "prefix_len": 0, "suffix_len": 4}, control_hz=10.0)
    ticks = iter([0.0, 0.3])  # 实测 0.3s × 10Hz = 3 步
    monkeypatch.setattr("motrix_edge.rtc.manager.time.monotonic", lambda: next(ticks))

    assert float(rtc.infer({"observations/qpos": np.zeros(DIM, dtype=np.float32)})[0]) == 1.0  # P=0 → 不跳
    status = rtc.status()
    assert status["last_delay"] == 0.3
    assert status["last_delay_steps"] == 3  # 参考值：P 建议设为 3
    assert status["last_chunk"]["lens"]["prefix"] == 0


def test_rtc_late_chunk_prefix_is_unavoidable(monkeypatch):
    """块起点已落后当前步号（异步 / 延迟）→ 那部分必在“过去”，仍会跳过（P 与之取大）。"""
    policy = _FakePolicy([[9, 9, 9, 9, 9]], start_offset=-2)  # 块从 index-2 起
    rtc = _rtc(policy, prefix_len=0, suffix_len=4)
    rtc.infer({"observations/qpos": np.zeros(DIM, dtype=np.float32)})
    assert rtc.status()["last_chunk"]["lens"]["prefix"] == 2


def test_validate_config_rejects_inconsistent_segments():
    """交叉约束：P+S<H、P+E+S<=H、E>P。"""
    base = {
        "enabled": True,
        "action_horizon": 10,
        "prefix_len": 2,
        "execution_horizon": 4,
        "suffix_len": 4,
        "aggregate_fn": "weighted_average",
    }
    assert validate_config(dict(base)) == base  # 2 + 4 + 4 = 10 ✓；E=4 > P=2 ✓
    with pytest.raises(ValueError, match="must be < action_horizon"):
        validate_config({**base, "suffix_len": 8})  # P + S = 10 >= H
    with pytest.raises(ValueError, match="must be <= action_horizon"):
        validate_config({**base, "execution_horizon": 6})  # 2 + 6 + 4 > 10
    with pytest.raises(ValueError, match="must be > prefix_len"):
        validate_config({**base, "execution_horizon": 2})  # E = 2 <= P = 2


def test_rtc_configure_rejects_inconsistent_merge():
    """运行期单键设置也做合并后校验（不会把配置改成自相矛盾的状态）。"""
    rtc = _rtc(_FakePolicy([[1] * 10]), action_horizon=10, prefix_len=2, suffix_len=4)
    with pytest.raises(ValueError, match="must be < action_horizon"):
        rtc.configure(suffix_len=18)


def test_rtc_slices_execution_and_suffix_lengths():
    """切分步数入 status：execution = E、suffix = S（供前端观测块结构）。"""
    policy = _FakePolicy([[1] * 6])
    rtc = _rtc(policy, suffix_len=2)  # E = 4
    rtc.infer({"observations/qpos": np.zeros(DIM, dtype=np.float32)})
    lens = rtc.status()["last_chunk"]["lens"]
    assert lens == {"prefix": 0, "execution": 4, "suffix": 2}


def test_rtc_execution_horizon_override():
    """实际块长 = H（常态）时显式 execution_horizon 原样生效（优先于「H - P - S」推导）。"""
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}
    rtc = _rtc(_FakePolicy([[1] * 6]), action_horizon=6, execution_horizon=3, suffix_len=2)
    rtc.infer(obs)
    assert rtc.status()["last_chunk"]["lens"] == {"prefix": 0, "execution": 3, "suffix": 2}

    exact = _rtc(_FakePolicy([[1] * 6]), action_horizon=6, execution_horizon=4, suffix_len=2)
    exact.infer(obs)
    assert exact.status()["last_chunk"]["lens"]["execution"] == 4


@pytest.mark.parametrize(
    ("height", "expected"),
    [
        (50, {"prefix": 0, "execution": 30, "suffix": 20}),  # 块长 = H → 与配置完全一致
        (40, {"prefix": 0, "execution": 24, "suffix": 16}),  # 0.8 倍
        (20, {"prefix": 0, "execution": 12, "suffix": 8}),  # 0.4 倍
    ],
)
def test_rtc_short_chunk_scales_segments(height, expected):
    """块长 < H 时 P/E/S 按 ``块长 / H`` 等比缩放（三段之和 = 实际块长）；块长 = H 时不变。"""
    policy = _FakePolicy([[float(i) for i in range(1, height + 1)]])
    rtc = _rtc(policy, action_horizon=50, prefix_len=0, execution_horizon=30, suffix_len=20)
    rtc.infer({"observations/qpos": np.zeros(DIM, dtype=np.float32)})
    assert rtc.status()["last_chunk"]["lens"] == expected


def test_rtc_short_chunk_prefetch_rate_is_scaled():
    """块长 16 < H=50 时预取提前量同样缩放：否则提前量 20 > 块长 → 每一拍都在推理。

    缩放后 E = 30 × 16/50 ≈ 10、提前量 = 6 → 60 步约拉 6 块（未缩放时要 60 次）。
    """
    policy = _FakePolicy([[float(i)] * 16 for i in range(1, 9)])
    rtc = _rtc(policy, action_horizon=50, prefix_len=0, execution_horizon=30, suffix_len=20)

    _drive(rtc, 60)
    assert 5 <= policy.calls <= 7
    assert rtc.status()["last_chunk"]["height"] == 16
    rtc.close()


def test_rtc_none_chunk_returns_none():
    """策略未返回动作（None）→ infer 返回 None（会话跳过本步，不升级为任务错误）。"""

    class _EmptyPolicy:
        def infer_chunk(self, observation, index=None):
            return None

    rtc = _rtc(_EmptyPolicy())
    assert rtc.infer({"observations/qpos": np.zeros(DIM, dtype=np.float32)}) is None


def test_rtc_disabled_fetches_every_step():
    """enabled=False：退化模式——每步请求一次、只取块首步（无块缓存 / 无平滑）。"""
    policy = _FakePolicy([[1, 1, 1], [2, 2, 2]])
    rtc = _rtc(policy, enabled=False)
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}
    values = [float(rtc.infer(obs)[0]) for _ in range(3)]
    assert values == [1.0, 2.0, 2.0]
    assert policy.calls == 3  # 每步都请求


def test_rtc_reset_clears_queue_and_index():
    policy = _FakePolicy([[1] * 6])
    rtc = _rtc(policy, suffix_len=1)
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}
    rtc.infer(obs)
    rtc.reset()
    status = rtc.status()
    assert status["index"] == 0
    assert status["remaining"] == 0
    assert status["fetches"] == 0
    assert status["last_chunk"] is None


# ---- 运行期参数 / 状态 ----------------------------------------------------------


def test_rtc_configure_applies_and_validates():
    policy = _FakePolicy([[1] * 6])
    rtc = _rtc(policy)
    params = rtc.configure(suffix_len=3, aggregate_fn="average")
    assert params["suffix_len"] == 3
    assert params["aggregate_fn"] == "average"
    with pytest.raises(ValueError):
        rtc.configure(suffix_len=-1)


def test_rtc_status_shape():
    policy = _FakePolicy([[1] * 6])
    rtc = _rtc(policy, suffix_len=2)
    status = rtc.status()
    assert status["enabled"] is True
    assert set(status["params"]) == {
        "enabled",
        "action_horizon",
        "prefix_len",
        "execution_horizon",
        "suffix_len",
        "aggregate_fn",
    }
    assert status["index"] == 0
    assert status["remaining"] == 0
    assert status["last_chunk"] is None


def test_rtc_status_reports_prefetch_health():
    """状态上报的预取健康度字段：区分「策略滞后（stale）」与「策略没数据（failed）」。"""
    rtc = _rtc(_FakePolicy([[1] * 6]), suffix_len=2)
    status = rtc.status()
    assert status["inflight"] is False
    assert status["last_error"] is None
    assert (status["fetches"], status["stale_chunks"], status["failed_chunks"]) == (0, 0, 0)
    rtc.infer(_obs())
    assert rtc.status()["fetches"] == 1  # 登记预取即计数
    assert rtc.status()["remaining"] == 5  # 首块 6 步、已下发 1 步


def test_ctor_rejects_unknown_config_key():
    """构造期配置键名写错 → ValueError（与 configure() 一致，不再静默忽略）。"""
    with pytest.raises(ValueError, match="unknown rtc param"):
        build_rtc(_FakePolicy([[1] * 6]), {"acton_horizon": 4})
    with pytest.raises(ValueError, match="unknown rtc param"):
        RTCManager(policy=_FakePolicy([[1] * 6]), config={"bogus": 1})


def test_disabled_mode_truncates_chunk_at_action_horizon():
    """退化模式也按 H 截断：超 H 的步一律视为过期（与 RTC 路径「超 H 即过期」同口径）。"""
    obs = _obs()
    blocks = [[9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]  # 模型给出 10 步块，且落后请求步号 3 步
    within = _rtc(_FakePolicy(blocks, start_offset=-3), action_horizon=4, enabled=False)
    assert float(within.infer(obs)[0]) == 6.0  # age=3 < H=4 → 取块内下标 3
    assert within.status()["last_chunk"]["height"] == 4  # 块长已按 H 截断（上报同口径）

    beyond = _rtc(_FakePolicy(blocks, start_offset=-3), action_horizon=2, enabled=False)
    assert beyond.infer(obs) is None  # age=3 >= H=2 → 过期，不下发深视界动作
    assert beyond.status()["stale_chunks"] == 1


def test_disabled_mode_skips_stale_actions():
    """enabled=False 也不下发过期动作：块首步落在过去 → 取块内当前步；整块过期 → None。"""
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}
    rtc = _rtc(_FakePolicy([[9, 8, 7, 6]], start_offset=-2), enabled=False)  # 块从 index-2 起
    assert float(rtc.infer(obs)[0]) == 7.0  # 当前步在块内下标 2 → 跳过两个过期步
    assert rtc.status()["stale_chunks"] == 0

    stale = _rtc(_FakePolicy([[9, 8, 7, 6]], start_offset=-10), enabled=False)
    assert stale.infer(obs) is None  # 整块都在过去 → 不下发
    assert stale.status()["stale_chunks"] == 1


def test_rtc_default_suffix_and_aggregate():
    """代码缺省：suffix_len 与 aggregate_fn 有默认值（不依赖配置）。"""
    rtc = RTCManager(policy=_FakePolicy([[1] * 6]))
    params = rtc.params
    assert params["enabled"] is True
    assert params["aggregate_fn"] == "weighted_average"
    assert params["suffix_len"] >= 0


# ---- 异步预取（线程 / 单飞 / 快照 / 作废 / 回收）----------------------------------


class _BlockingPolicy:
    """可控 fake policy：第 ``block_from`` 次调用起阻塞在 ``infer_chunk``（模拟推理耗时）。

    - ``start_offsets``：逐次调用的块首步偏移（缺省 0 = 回显请求步号）；
    - ``observations``：每次调用在**阻塞解除后**拷下的 qpos，用于验证观测快照。
    """

    def __init__(self, blocks, block_from=1, start_offsets=None):
        self._blocks = [list(b) for b in blocks]
        self._block_from = block_from
        self._start_offsets = list(start_offsets) if start_offsets is not None else []
        self.calls = 0
        self.indices: list[int] = []
        self.observations: list[np.ndarray] = []
        self.entered = threading.Event()  # 已进入阻塞（工作线程开始推理）
        self.release = threading.Event()  # 允许返回

    def infer_chunk(self, observation, index=None):
        self.calls += 1
        self.indices.append(index)
        if self.calls >= self._block_from:
            self.entered.set()
            if not self.release.wait(ASYNC_TIMEOUT):
                raise AssertionError("fake policy never released")
        self.observations.append(np.copy(observation["observations/qpos"]))
        spec = self._blocks[min(self.calls - 1, len(self._blocks) - 1)]
        offset = self._start_offsets[self.calls - 1] if self.calls <= len(self._start_offsets) else 0
        return ActionChunk(actions=np.array([[float(v)] * DIM for v in spec]), start_index=int(index or 0) + offset)


def test_async_prefetch_does_not_block_and_keeps_single_flight():
    """异步预取：``infer()`` 不被推理阻塞；一块在途时不重复发起（单飞）。"""
    policy = _BlockingPolicy([[1] * 6, [2] * 6], block_from=2)
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)  # E = 4，预取提前量 = 2
    obs = _obs()

    assert [float(rtc.infer(obs)[0]) for _ in range(5)] == [1.0] * 5  # 步 0..4：第 5 步（index=4）发起预取
    assert policy.entered.wait(ASYNC_TIMEOUT)  # 工作线程已进入推理
    assert rtc.status()["inflight"] is True
    assert float(rtc.infer(obs)[0]) == 1.0  # 推理阻塞中：控制环照常拿到队内动作
    assert policy.calls == 2  # 单飞：在途期间不再叠加请求
    assert rtc.status()["inflight"] is True

    policy.release.set()
    assert _wait_until(lambda: rtc.status()["inflight"] is False)
    assert rtc.status()["last_chunk"]["start_index"] == 4  # 第二块已按当时步号落块
    assert float(rtc.infer(obs)[0]) == 2.0  # 新块从当前步号接上
    rtc.close()


def test_async_prefetch_skips_steps_elapsed_during_inference():
    """落块按**当时**步号切分：推理期间控制环继续跑 → 那几步自动计为 prefix（真实过期步）。"""
    policy = _BlockingPolicy([[1] * 10, [2] * 10], block_from=2)
    rtc = _rtc(policy, action_horizon=10, suffix_len=5)  # E = 5，预取提前量 = 5
    obs = _obs()

    assert [float(rtc.infer(obs)[0]) for _ in range(6)] == [1.0] * 6  # 步 0..5：第 6 步（index=5）发起预取
    assert policy.entered.wait(ASYNC_TIMEOUT)
    assert [float(rtc.infer(obs)[0]) for _ in range(4)] == [1.0] * 4  # 步 6..9：推理未回，继续吃旧块
    assert rtc.status()["index"] == 10

    policy.release.set()
    assert _wait_until(lambda: rtc.status()["inflight"] is False)
    assert rtc.status()["last_chunk"]["start_index"] == 5
    assert rtc.status()["last_chunk"]["lens"]["prefix"] == 5  # 块首步 5 已过期 5 步（自动跳过）
    assert float(rtc.infer(obs)[0]) == 2.0  # 从当前步号接上
    rtc.close()


def test_async_prefetch_snapshots_observation():
    """观测在发起预取时快照：调用方随后覆写原 buffer（共享内存被进程覆写）不影响工作线程输入。"""
    policy = _BlockingPolicy([[1] * 6, [2] * 6], block_from=2)
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)
    qpos = np.zeros(DIM, dtype=np.float32)
    obs = {"observations/qpos": qpos}  # 复用同一个 dict / 数组（SHM 视图的真实形态）

    for _ in range(5):
        rtc.infer(obs)  # 步 4 发起异步预取（此刻快照已拷走）
    assert policy.entered.wait(ASYNC_TIMEOUT)
    qpos[:] = 9.0  # 覆写原 buffer

    policy.release.set()
    assert _wait_until(lambda: rtc.status()["inflight"] is False)
    assert policy.observations[1] == pytest.approx([0.0, 0.0])  # 读到的是快照，不是 9.0
    rtc.close()


def test_async_policy_error_keeps_worker_alive():
    """异步策略异常：不外抛（工作线程存活）、经 ``status().last_error`` 暴露，下一块照常。"""

    class _FlakyPolicy(_FakePolicy):
        def infer_chunk(self, observation, index=None):
            self.calls += 1
            self.indices.append(index)
            if self.calls == 2:
                raise RuntimeError("boom")
            spec = self._blocks[min(self.calls - 1, len(self._blocks) - 1)]
            return ActionChunk(actions=np.array([[float(v)] * DIM for v in spec]), start_index=int(index or 0))

    policy = _FlakyPolicy([[1] * 6, [7] * 6])
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)
    obs = _obs()

    assert [float(rtc.infer(obs)[0]) for _ in range(5)] == [1.0] * 5  # 第 5 步发起预取 → 工作线程抛错
    assert _wait_until(lambda: rtc.status()["last_error"] is not None)
    status = rtc.status()
    assert "RuntimeError: boom" in status["last_error"]
    assert status["failed_chunks"] == 1
    assert status["inflight"] is False  # 单飞标记已清 → 不会卡住后续预取

    rtc.infer(obs)  # 重新发起（call 3 正常）
    assert _wait_until(lambda: rtc.status()["last_chunk"]["start_index"] == 5)
    assert rtc.status()["failed_chunks"] == 1
    assert float(rtc.infer(obs)[0]) == 7.0  # 新块可用
    rtc.close()


def test_async_non_finite_chunk_keeps_worker_alive():
    """策略返回 NaN 块 → 与策略异常同款：不外抛、计 ``failed_chunks``、线程存活、下一块照常。"""

    class _NanPolicy(_FakePolicy):
        def infer_chunk(self, observation, index=None):
            self.calls += 1
            self.indices.append(index)
            spec = [float("nan")] * 6 if self.calls == 2 else self._blocks[min(self.calls - 1, len(self._blocks) - 1)]
            return ActionChunk(actions=np.array([[v] * DIM for v in spec]), start_index=int(index or 0))

    policy = _NanPolicy([[1] * 6, [7] * 6])
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)
    obs = _obs()

    assert [float(rtc.infer(obs)[0]) for _ in range(5)] == [1.0] * 5  # 第 5 步发起预取 → 工作线程拿到 NaN
    assert _wait_until(lambda: rtc.status()["last_error"] is not None)
    status = rtc.status()
    assert "non-finite" in status["last_error"]
    assert status["failed_chunks"] == 1
    assert status["inflight"] is False  # 单飞槽已释放 → 不会卡住后续预取

    rtc.infer(obs)  # 重新发起（call 3 正常）
    assert _wait_until(lambda: rtc.status()["last_chunk"]["start_index"] == 5)
    assert rtc.status()["failed_chunks"] == 1
    assert float(rtc.infer(obs)[0]) == 7.0  # 新块可用
    rtc.close()


def test_worker_survives_apply_failure(monkeypatch):
    """落块抛意外：工作线程必须活下来、单飞槽必须释放（否则之后再也预取不了）。"""
    policy = _FakePolicy([[1] * 6, [2] * 6, [3] * 6, [4] * 6])
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)
    # 只 patch 本实例（patch 类会被前面用例遗留的 manager 调到，计数就不准了）
    original = rtc._apply_locked
    calls = {"n": 0}
    worker: dict = {}

    def boom(chunk):
        calls["n"] += 1
        if calls["n"] == 2:  # 第 2 次落块 = 工作线程那次
            worker["thread"] = threading.current_thread()
            raise RuntimeError("apply boom")
        return original(chunk)

    monkeypatch.setattr(rtc, "_apply_locked", boom)
    _drive(rtc, 6)  # 第 5 步发起预取 → 工作线程落块时抛意外

    status = rtc.status()
    assert status["failed_chunks"] == 1  # 落块意外也算一次失败
    assert status["last_error"] is not None and "apply boom" in status["last_error"]
    assert status["inflight"] is False  # 单飞槽已由 finally 结清
    assert worker["thread"].is_alive()  # 抛意外的那条预取线程还活着（不依赖全局线程计数）

    _drive(rtc, 6)  # 之后仍能继续预取（槽没漏、线程没死）
    assert policy.calls >= 3
    rtc.close()


def test_async_fully_stale_chunk_is_dropped_and_counted():
    """响应回来时整块都在过去（块长 < 推理期间走过的步数）→ 丢弃 + ``stale_chunks`` 计数。"""
    policy = _BlockingPolicy([[1] * 10, [2] * 2], block_from=2, start_offsets=[0, -20])
    rtc = _rtc(policy, action_horizon=10, suffix_len=5)
    obs = _obs()

    assert [float(rtc.infer(obs)[0]) for _ in range(6)] == [1.0] * 6  # 第 6 步（index=5）发起预取
    assert policy.entered.wait(ASYNC_TIMEOUT)
    assert [float(rtc.infer(obs)[0]) for _ in range(4)] == [1.0] * 4
    policy.release.set()
    assert _wait_until(lambda: rtc.status()["inflight"] is False)

    status = rtc.status()
    assert status["stale_chunks"] == 1  # 整块过期 → 丢弃并计数（不是静默无动作）
    assert status["failed_chunks"] == 0
    assert status["last_chunk"]["start_index"] == -15  # 响应回来时整块已在过去
    assert float(rtc.infer(obs)[0]) == 2.0  # 队内无步 → 本步内联取一块（断流恢复）
    rtc.close()


def test_reset_discards_inflight_result():
    """``reset()`` 作废在途预取：响应回来后按世代丢弃，不污染新会话的队列 / 步号。"""
    policy = _BlockingPolicy([[1] * 6, [2] * 6], block_from=2)
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)
    obs = _obs()

    for _ in range(5):
        rtc.infer(obs)  # 步 4 发起异步预取（在途）
    assert policy.entered.wait(ASYNC_TIMEOUT)

    rtc.reset()  # 会话复位：作废结果，但不释放单飞槽（请求还在策略端跑）
    assert rtc.status()["inflight"] is True
    policy.release.set()
    time.sleep(0.05)  # 等响应回来（世代不符 → 丢弃，并释放单飞槽）
    status = rtc.status()
    assert status["index"] == 0
    assert status["remaining"] == 0
    assert status["last_chunk"] is None  # 在途结果被丢弃，没污染新会话
    assert status["last_delay"] == 0  # 旧世代的实测耗时同样不计入
    assert status["inflight"] is False
    rtc.close()


def test_reset_discards_late_failure_stats():
    """``reset()`` 之后迟到的异常按世代作废：不计数、不写 ``last_error``（不污染新会话）。

    失败路径曾在世代检查之外记账：旧会话的超时报错回来，会把新会话的 ``failed_chunks`` / ``last_error``
    占住——而 reset 正是「策略出问题后重开会话」的时刻，这两个字段恰恰最常被看。
    """
    policy = _FakePolicy([[1] * 6, [2] * 6])
    entered, release = threading.Event(), threading.Event()
    original = policy.infer_chunk
    calls = {"n": 0}

    def flaky(observation, index=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            entered.set()
            release.wait(ASYNC_TIMEOUT)
            raise RuntimeError("late boom")
        return original(observation, index=index)

    policy.infer_chunk = flaky
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)
    for _ in range(5):
        rtc.infer(_obs())  # 第 5 步发起预取 → 工作线程进入阻塞
    assert entered.wait(ASYNC_TIMEOUT)

    rtc.reset()
    release.set()
    assert _wait_until(lambda: rtc.status()["inflight"] is False)
    status = rtc.status()
    assert status["failed_chunks"] == 0  # 旧世代的异常不计入新会话
    assert status["last_error"] is None
    assert status["last_delay"] == 0
    rtc.close()


def test_infer_direct_keeps_bookkeeping_under_lock(monkeypatch):
    """退化模式的簿记在锁内：``_drop_stale_locked`` 的契约是「持锁调用」，状态注释也说全部由 ``_lock`` 保护。"""
    rtc = _rtc(_FakePolicy([[1] * 6]), enabled=False)
    seen: dict = {}
    original = RTCManager._drop_stale_locked

    def probe(self, chunk):
        seen["locked"] = self._lock._is_owned()  # CPython 私有 API：RLock 当前是否被持有
        return original(self, chunk)

    monkeypatch.setattr(RTCManager, "_drop_stale_locked", probe)
    assert float(rtc.infer(_obs())[0]) == 1.0  # 退化模式正常取到块首步
    assert seen.get("locked") is True


def test_disabled_mode_discards_result_when_reset_during_call():
    """``enabled=False`` 且在调用期间被 reset：本轮结果与统计都不采用（步号 / 上报不被旧会话污染）。"""
    policy = _BlockingPolicy([[1] * 6], block_from=1)  # 首次调用即阻塞
    rtc = _rtc(policy, enabled=False)
    out: dict = {}

    def call():
        out["action"] = rtc.infer(_obs())

    thread = threading.Thread(target=call)
    thread.start()
    assert policy.entered.wait(ASYNC_TIMEOUT)
    rtc.reset()  # 调用进行中被复位
    policy.release.set()
    thread.join(ASYNC_TIMEOUT)

    assert out["action"] is None  # 旧世代结果不采用
    status = rtc.status()
    assert status["index"] == 0
    assert status["last_chunk"] is None
    assert status["fetches"] == 0
    assert status["last_delay"] == 0


def test_reset_does_not_start_concurrent_request():
    """reset 后立刻步进：在途请求回来前不叠出第二个请求（策略端同一时刻只处理一个）。"""
    policy = _BlockingPolicy([[1] * 6, [2] * 6], block_from=2)
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)

    for _ in range(5):
        rtc.infer(_obs())  # 步 4 发起异步预取（在途）
    assert policy.entered.wait(ASYNC_TIMEOUT)
    assert policy.calls == 2

    rtc.reset()
    assert rtc.infer(_obs()) is None  # 队列已清空 + 单飞槽被占 → 本步跳过
    assert policy.calls == 2  # 没有并发发起第二个请求

    policy.release.set()
    assert _wait_until(lambda: rtc.status()["inflight"] is False)
    assert float(rtc.infer(_obs())[0]) == 2.0  # 槽释放后可正常续上
    assert policy.calls == 3
    rtc.close()


def test_reset_releases_claim_when_job_not_taken_yet():
    """``reset()`` 撞上「请求已登记、工作线程还没取走」：这次调用从没到过策略端，不能留下占用。

    否则 ``infer()`` 之后每步都返回 ``None``（策略明明有数据），而且再 reset 也救不回来。
    """
    policy = _FakePolicy([[1] * 6, [2] * 6])
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)
    release = threading.Event()
    worker = RTCManager._worker

    def stalled_worker():
        release.wait(ASYNC_TIMEOUT)  # 卡在「尚未取走登记」的窗口里
        worker(rtc)

    rtc._worker = stalled_worker
    for _ in range(5):
        rtc.infer(_obs())  # 第 5 步把请求登记进 _job（工作线程被卡住，尚未取走）
    assert rtc.status()["inflight"] is True
    assert policy.calls == 1  # 首块是内联取的，登记的这次还没发出去

    rtc.reset()
    assert rtc.status()["inflight"] is False  # 修复前恒为 True（永久停摆）
    assert float(rtc.infer(_obs())[0]) == 2.0  # 仍能续上新块
    release.set()
    rtc.close()


def test_close_releases_claim_when_job_not_taken_yet():
    """``close()`` 同源：没被取走的登记也要作废，且关闭后仍可内联取块。"""
    policy = _FakePolicy([[1] * 6, [2] * 6])
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)
    release = threading.Event()
    worker = RTCManager._worker

    def stalled_worker():
        release.wait(ASYNC_TIMEOUT)
        worker(rtc)

    rtc._worker = stalled_worker
    for _ in range(5):
        rtc.infer(_obs())
    assert rtc.status()["inflight"] is True

    rtc.close(timeout=0.05)  # 工作线程还卡着，不等它
    assert rtc.status()["inflight"] is False
    # 关闭后没有后台预取，队列耗尽时转内联取块；槽若被永久占用，这里会一路返回 None
    values = [rtc.infer(_obs()) for _ in range(8)]
    assert all(value is not None for value in values)
    assert policy.calls == 3  # 内联又拉了 2 块（绝对步号 5 / 9）
    release.set()


def test_rtc_queue_is_isolated_from_policy_buffer():
    """入队动作不与策略侧数组共享内存：策略复用 / 覆写自己的 buffer 不影响已入队动作。"""

    class _ReuseBufferPolicy:
        """所有块共用同一块 buffer（网络层解码缓冲的真实形态），每次调用覆写它。"""

        def __init__(self):
            self.buffer = np.zeros((6, DIM), dtype=float)
            self.calls = 0

        def infer_chunk(self, observation, index=None):
            self.calls += 1
            self.buffer[:] = float(self.calls)
            return ActionChunk(actions=self.buffer, start_index=int(index or 0))

    policy = _ReuseBufferPolicy()
    rtc = _rtc(policy, suffix_len=1)  # 块长 6 / E = 5 / 预取提前量 = 1
    assert _drive(rtc, 6) == [1.0] * 6  # 第 6 步触发预取 → 块 2（值 2.0）已入队
    assert policy.calls == 2

    policy.buffer[:] = 99.0  # 策略下一轮复用 buffer
    assert _drive(rtc, 3) == [2.0, 2.0, 2.0]  # 队内动作是自己的拷贝，不是 buffer 视图
    rtc.close()


def test_close_stops_prefetch_thread_and_infer_degrades_inline():
    """``close()`` 回收工作线程（幂等）；之后 ``infer()`` 不再启动线程（退化为内联预取）。"""
    policy = _FakePolicy([[1] * 6, [2] * 6])
    rtc = _rtc(policy, action_horizon=6, suffix_len=2)
    before = len(_prefetch_threads())

    _drive(rtc, 5)  # 第 5 步触发异步预取 → 工作线程启动
    assert len(_prefetch_threads()) == before + 1

    rtc.close()
    assert len(_prefetch_threads()) == before
    rtc.close()  # 幂等
    # 已关闭 → 预取退化为**内联**完成（不再起线程）：继续步进到触发点，队列被内联续上
    assert [float(rtc.infer(_obs())[0]) for _ in range(5)] == [pytest.approx(1.7), 2.0, 2.0, 2.0, 2.0]
    assert rtc.status()["fetches"] == 3  # 第 3 次请求由内联完成
    assert rtc.status()["remaining"] > 0  # 队列已续上
    assert len(_prefetch_threads()) == before
    rtc.close()
