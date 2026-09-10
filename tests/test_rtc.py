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

覆盖：动作块三元切分（prefix / execution / suffix）、时序平滑（重叠加权融合）、预取时机、
步号推进、prefix（已失效）丢弃、enabled=False 退化、运行期参数校验 / 更新、状态上报。
"""

import numpy as np
import pytest

from motrix_edge.rtc import ActionChunk, RTCManager, build_rtc, validate_config, validate_params
from motrix_edge.rtc.base import ChunkSlice

DIM = 2


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
    return build_rtc(policy, {"enabled": True, "suffix_len": 1, "aggregate_fn": "weighted_average", **config})


# ---- ActionChunk / ChunkSlice -------------------------------------------------


def test_action_chunk_normalizes_single_step():
    """单步动作（[dim]）规范化为 [1, dim]。"""
    chunk = ActionChunk(actions=np.array([1.0, 2.0]))
    assert chunk.height == 1
    assert chunk.dim == 2
    assert chunk.start_index == 0


def test_action_chunk_rejects_bad_shape():
    with pytest.raises(ValueError, match="2-D"):
        ActionChunk(actions=np.zeros((2, 2, 2)))


def test_action_chunk_slice_three_way():
    """三元切分：prefix（过去）/ execution（执行）/ suffix（过渡）按步数连续切分。"""
    chunk = ActionChunk(actions=np.arange(10 * DIM, dtype=float).reshape(10, DIM), start_index=0)
    split = chunk.slice(prefix_len=2, execution_len=5, suffix_len=3)
    assert isinstance(split, ChunkSlice)
    assert split.lens == {"prefix": 2, "execution": 5, "suffix": 3}
    assert np.array_equal(split.prefix, chunk.actions[0:2])
    assert np.array_equal(split.execution, chunk.actions[2:7])
    assert np.array_equal(split.suffix, chunk.actions[7:10])


def test_action_chunk_slice_clamps_overflow():
    """步数超过块长时截断（execution / suffix 依次吃满剩余）。"""
    chunk = ActionChunk(actions=np.zeros((4, DIM)), start_index=0)
    split = chunk.slice(prefix_len=1, execution_len=99, suffix_len=99)
    assert split.lens == {"prefix": 1, "execution": 3, "suffix": 0}


# ---- 参数校验 -----------------------------------------------------------------


def test_validate_params_ok_and_unknown_key():
    assert validate_params({"suffix_len": 5, "enabled": False, "aggregate_fn": "latest_only"}) == {
        "suffix_len": 5,
        "enabled": False,
        "aggregate_fn": "latest_only",
    }
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
    """一个块支撑 execution 段多步：块内不为每步重复请求（预取只在进入 suffix 窗口时）。"""
    policy = _FakePolicy([[1, 1, 1, 1, 1, 1]])  # H=6
    rtc = _rtc(policy, suffix_len=2)  # E = H - S = 4
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}

    actions = [float(rtc.infer(obs)[0]) for _ in range(4)]
    assert actions == [1.0] * 4
    assert policy.calls == 1  # 4 步只拉 1 块（剩余 2 步时下一次 infer 会预取）
    assert rtc.status()["index"] == 4
    assert policy.indices[0] == 0  # 首次以当前绝对步号请求


def test_rtc_prefetches_when_entering_suffix_window():
    """队列剩余 <= suffix_len 时提前同步预取下一块（保证不断流）。"""
    policy = _FakePolicy([[1, 1, 1], [2, 2, 2]])  # H=3
    rtc = _rtc(policy, suffix_len=1)  # E = 2
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}

    values = [float(rtc.infer(obs)[0]) for _ in range(4)]
    assert policy.calls == 2  # 第 3 步（剩余 1 <= S）时预取
    assert policy.indices == [0, 2]  # 预取时绝对步号已推进到 2
    # ts2 重叠：0.3*旧(1) + 0.7*新(2) = 1.7
    assert values[:3] == [1.0, 1.0, pytest.approx(1.7)]
    assert values[3] == pytest.approx(2.0)


def test_rtc_temporal_smoothing_weighted_sequence():
    """时序平滑完整序列（原 act 平滑用例迁移）：K=3、S=1、块值 1..N。"""
    policy = _FakePolicy([[1, 1, 1], [2, 2, 2], [3, 3, 3], [4, 4, 4], [5, 5, 5], [6, 6, 6]])
    rtc = _rtc(policy, suffix_len=1)  # E = 3 - 1 = 2
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}

    actions = [float(rtc.infer(obs)[0]) for _ in range(8)]
    assert actions == [1.0, 1.0, pytest.approx(1.7), 2.0, pytest.approx(2.7), 3.0, pytest.approx(3.7), 4.0]
    assert policy.calls == 4  # 预取发生在 ts 0/2/4/6（每 K-S=2 步一次）


def test_rtc_aggregate_latest_only():
    """aggregate_fn=latest_only：重叠步直接取新块值。"""
    policy = _FakePolicy([[1, 1, 1], [2, 2, 2]])
    rtc = _rtc(policy, suffix_len=1, aggregate_fn="latest_only")
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}
    values = [float(rtc.infer(obs)[0]) for _ in range(4)]
    assert values == [1.0, 1.0, 2.0, 2.0]


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
    """**执行段还剩 P 步时发请求**：推理耗时的 P 步吃掉执行段尾巴，后缀段与下一块重叠加权平均。

    H=10 / P=2 / S=4（E = 4），块值 = 块号×100 + 块内步号（块 1/2/3 = 100s/200s/300s，起始下标 0/4/8）：

    - 块 1：跳 P=2 → 执行段 = 下标 2..5（102..105），后缀段 = 下标 6..9（106..109）留在队列；
    - 下标 4（执行段还剩 P=2 步，队列剩 P+S=6 步）→ 发请求；
    - 块 2 到达：跳 P=2 → 从下标 6 入队 → 与块 1 后缀段重叠的 4 步（= S）加权平均，
      其中下标 6/7 由本循环下发、下标 8/9 在**下一次推理期间被机器人执行**（故不出现在 infer 回值里）；
    - 下个请求在下标 8（块 2 执行段还剩 P=2 步）→ 如此每 E-P=2 步一个块。
    """
    policy = _FakePolicy([[100 + i for i in range(10)], [200 + i for i in range(10)], [300 + i for i in range(10)]])
    rtc = _rtc(policy, action_horizon=10, prefix_len=2, suffix_len=4)  # E = 10 - 2 - 4 = 4
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}

    values = [float(rtc.infer(obs)[0]) for _ in range(6)]
    assert values[0:2] == [102.0, 103.0]  # 块 1 执行段未重叠部分：直接执行
    assert values[2:4] == [pytest.approx(0.3 * 106 + 0.7 * 202), pytest.approx(0.3 * 107 + 0.7 * 203)]
    assert values[4:6] == [pytest.approx(0.3 * 206 + 0.7 * 302), pytest.approx(0.3 * 207 + 0.7 * 303)]
    assert policy.indices == [0, 4, 8]  # 每次都在「执行段还剩 P=2 步」时请求
    assert rtc.status()["last_chunk"]["lens"] == {"prefix": 2, "execution": 4, "suffix": 4}


def test_rtc_prefetch_trigger_is_prefix_plus_suffix():
    """预取触发点 = 「执行段还剩 P 步」→ ``remaining <= P + S``（不是后缀段开始）。"""
    policy = _FakePolicy([[float(i) for i in range(1, 21)], [float(i) for i in range(101, 121)]])
    rtc = _rtc(policy, action_horizon=20, prefix_len=2, suffix_len=4)  # E = 20 - 2 - 4 = 14
    obs = {"observations/qpos": np.zeros(DIM, dtype=np.float32)}

    for _ in range(12):  # 下标 2..13：remaining 18..7 > P+S=6 → 不拉块
        rtc.infer(obs)
    assert policy.calls == 1
    rtc.infer(obs)  # 下标 14：remaining = 6 <= P+S → 执行段（下标 2..15）还剩 P=2 步时拉下一块
    assert policy.calls == 2
    assert policy.indices == [0, 14]


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
    """显式 execution_horizon 优先于「H - suffix_len」。"""
    policy = _FakePolicy([[1] * 6])
    rtc = _rtc(policy, execution_horizon=6, suffix_len=2)
    rtc.infer({"observations/qpos": np.zeros(DIM, dtype=np.float32)})
    assert rtc.status()["last_chunk"]["lens"]["execution"] == 6


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


def test_rtc_default_suffix_and_aggregate():
    """代码缺省：suffix_len 与 aggregate_fn 有默认值（不依赖配置）。"""
    rtc = RTCManager(policy=_FakePolicy([[1] * 6]))
    params = rtc.params
    assert params["enabled"] is True
    assert params["aggregate_fn"] == "weighted_average"
    assert params["suffix_len"] >= 0
