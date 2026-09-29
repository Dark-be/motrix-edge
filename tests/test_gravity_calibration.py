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

"""重力标定纯逻辑的单测（``robot-pipeline/src/robot/gravity.py`` + 现场脚本可导入性）。

标定脚本依赖硬件 SDK（``pyAgxArm``），本机没有；所以这里**按文件路径加载模块**，只测不碰硬件的
那部分：位形规划（含限位过滤）、判稳、窗口统计、摩擦估计、样本汇总与 JSON 往返。这些逻辑一旦
写错，现场表现为「数据看着有、其实全错」，值得钉住。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROBOT_PIPELINE = REPO_ROOT / "robot-pipeline"
GRAVITY_PATH = ROBOT_PIPELINE / "src" / "robot" / "gravity.py"
SCRIPT_PATH = ROBOT_PIPELINE / "scripts" / "verify_gravity.py"

DOF = 6


def _load_by_path(path: Path, name: str):
    """按文件路径加载模块（绕开包 ``__init__``，不要求环境里有 SDK）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gravity():
    return _load_by_path(GRAVITY_PATH, "gravity_under_test")


# ---- 位形规划 ---------------------------------------------------------------


def test_plan_waypoints_moves_one_joint_at_a_time(gravity):
    """逐关节 ±span：每个点位只与基准位形差**一个**关节，且不生成多关节组合（combined=0）。"""
    home = np.zeros(DOF)
    points, dropped = gravity.plan_waypoints(home, span=0.3)

    assert dropped == []
    assert len(points) == 2 * DOF
    labels = [label for label, _ in points]
    assert labels[:2] == ["j1+", "j1-"]
    for label, q in points:
        delta = q - home
        assert np.count_nonzero(np.abs(delta) > 0) == 1, label
        assert np.isclose(np.abs(delta).max(), 0.3)


def test_plan_waypoints_drops_out_of_limit_points(gravity):
    """越界的点位一个都不返回（现场越界下发会被 SDK 拒绝 / 报警）：只在 dropped 里留名字。"""
    home = np.zeros(DOF)
    limits = np.tile(np.array([-0.1, 0.1]), (DOF, 1))  # 只留 ±0.1 的余量

    points, dropped = gravity.plan_waypoints(home, span=0.5, limits=limits)

    assert points == []
    assert len(dropped) == 2 * DOF


def test_plan_waypoints_combined_is_deterministic_and_within_limits(gravity):
    """多关节组合点：同种子可复现、受限位约束、偏移幅度在 ±span/2 内。"""
    home = np.full(DOF, 0.5)
    limits = np.tile(np.array([-1.0, 1.0]), (DOF, 1))

    first, _ = gravity.plan_waypoints(home, span=0.4, limits=limits, combined=3, seed=7)
    second, _ = gravity.plan_waypoints(home, span=0.4, limits=limits, combined=3, seed=7)
    other, _ = gravity.plan_waypoints(home, span=0.4, limits=limits, combined=3, seed=8)

    def combos(points):
        return [q for label, q in points if label.startswith("combo")]

    combo_first = combos(first)
    assert len(combo_first) == 3
    assert np.allclose(np.stack(combo_first), np.stack(combos(second)))
    assert not np.allclose(np.stack(combo_first), np.stack(combos(other)))
    for q in combo_first:
        assert np.all(np.abs(q - home) <= 0.2 + 1e-12)


# ---- 判稳 -------------------------------------------------------------------


def test_settle_detector_needs_consecutive_quiet_ticks(gravity):
    """判稳要求**连续** K 拍安静：中途有一拍抖动就重新计数。"""
    detector = gravity.SettleDetector({"ticks": 3})
    quiet = (np.zeros(DOF), np.zeros(DOF), np.zeros(DOF))

    assert detector.update(*quiet) is False
    assert detector.update(*quiet) is False
    detector.update(np.full(DOF, 0.05), np.zeros(DOF), np.zeros(DOF))  # 位移跳一下 → 计数清零
    assert detector.update(*quiet) is False
    assert detector.update(*quiet) is False
    assert detector.update(*quiet) is False
    assert detector.update(*quiet) is True
    assert detector.stable is True
    assert detector.update(*quiet) is True  # 判稳后保持

    detector.reset()
    assert detector.stable is False
    assert detector.update(*quiet) is False


def test_settle_detector_uses_worst_joint(gravity):
    """任一关节没停稳就不算稳（逐关节取最大）：单个关节的速度 / 力矩尖峰都要拦住。"""
    detector = gravity.SettleDetector({"ticks": 2, "vel": 0.01, "dtau": 0.01})
    q = np.zeros(DOF)
    tau = np.zeros(DOF)
    assert detector.update(q, np.zeros(DOF), tau) is False

    vel = np.zeros(DOF)
    vel[3] = 0.5  # 只有第 4 个关节在动
    assert detector.update(q, vel, tau) is False

    tau_bumped = tau.copy()
    tau_bumped[1] = 0.5  # 只有第 2 个关节力矩跳变
    assert detector.update(q, np.zeros(DOF), tau_bumped) is False
    assert detector.update(q, np.zeros(DOF), tau_bumped) is False
    assert detector.update(q, np.zeros(DOF), tau.copy()) is False
    assert detector.update(q, np.zeros(DOF), tau.copy()) is False
    assert detector.update(q, np.zeros(DOF), tau.copy()) is True


# ---- 窗口统计 / 摩擦 / 汇总 --------------------------------------------------


def test_window_stats_matches_manual_mean(gravity):
    """窗口统计 = 逐关节均值 + 标准差 + 速度峰值（现场判读与拟合都吃这几个数）。"""
    records = [
        {"q": np.array([1.0] * DOF), "vel": np.zeros(DOF), "tau": np.array([2.0] * DOF)},
        {"q": np.array([1.5] * DOF), "vel": np.array([0.1] * DOF), "tau": np.array([4.0] * DOF)},
        {"q": np.array([1.25] * DOF), "vel": np.zeros(DOF), "tau": np.array([3.0] * DOF)},
    ]
    stats = gravity.window_stats(records)

    assert stats["n"] == 3
    assert np.allclose(stats["q"], 1.25)
    assert np.allclose(stats["tau"], 3.0)
    assert np.allclose(stats["tau_std"], np.std([2.0, 4.0, 3.0]))
    assert stats["vel_absmax"] == pytest.approx(0.1)


def test_window_stats_rejects_empty_window(gravity):
    """空窗口直接报错（读数一直失败时应丢弃该点，而不是当成 0 力矩采进去）。"""
    with pytest.raises(ValueError, match="at least one record"):
        gravity.window_stats([])


def test_friction_from_passes_halves_the_delta(gravity):
    """正反两遍配对：`|τ_fwd − τ_rev| / 2` = 库仑摩擦幅值。"""
    q = np.array([0.2, 0.1, 0.0, 0.0, 0.0, 0.0])
    friction = 0.3
    forward = [{"q": q, "tau": np.full(DOF, 5.0 + friction)}]
    backward = [{"q": q + 1e-3, "tau": np.full(DOF, 5.0 - friction)}]

    estimate = gravity.friction_from_passes(forward, backward)

    assert estimate["paired"] == 1
    assert np.allclose(estimate["per_joint"], friction)
    assert estimate["max"] == pytest.approx(friction)


def test_friction_from_passes_skips_unmatched_pairs(gravity):
    """位形差得远的样本不配对（正反两遍必须落在同一个位形上才有意义）。"""
    forward = [{"q": np.zeros(DOF), "tau": np.zeros(DOF)}]
    backward = [{"q": np.full(DOF, 1.0), "tau": np.ones(DOF)}]

    estimate = gravity.friction_from_passes(forward, backward)

    assert estimate["paired"] == 0
    assert np.allclose(estimate["per_joint"], 0.0)


def test_summarize_samples_flags_overflow_and_reports_spread(gravity):
    """汇总要点：最大 |τ|（含关节号）、是否超 ±16 N·m、每关节行程。"""
    samples = [
        {"label": "a", "q": np.zeros(DOF), "tau": np.array([1.0, -20.0, 0, 0, 0, 0]), "tau_std": np.zeros(DOF)},
        {"label": "b", "q": np.full(DOF, 0.5), "tau": np.zeros(DOF), "tau_std": np.full(DOF, 0.01)},
    ]
    summary = gravity.summarize_samples(samples)

    assert summary["n"] == 2
    assert summary["tau_abs_max"] == pytest.approx(20.0)
    assert summary["tau_abs_max_joint"] == 2
    assert summary["exceeds_t_ff_limit"] is True
    assert summary["tau_abs_max_headroom"] == pytest.approx(gravity.T_FF_LIMIT_NM - 20.0)
    assert np.allclose(summary["q_spread"][0], 0.0)
    assert np.allclose(summary["q_spread"][1], 0.5)


def test_save_and_load_samples_round_trip(gravity, tmp_path):
    """样本 JSON 往返：``np.ndarray`` 落盘后能原样读回（拟合阶段的输入契约）。"""
    payload = {
        "version": 1,
        "samples": [{"label": "j1+", "q": np.zeros(DOF), "tau": np.arange(DOF, dtype=float)}],
        "summary": {"q_spread": np.ones((2, DOF))},
    }
    path = gravity.save_samples(tmp_path / "nested" / "samples.json", payload)

    loaded = gravity.load_samples(path)
    assert loaded["samples"][0]["tau"] == list(range(DOF))
    assert loaded["summary"]["q_spread"] == [[1.0] * DOF, [1.0] * DOF]


# ---- 现场脚本 -----------------------------------------------------------------


def test_field_script_imports_without_sdk(gravity):
    """``scripts/verify_gravity.py`` 顶层不 import SDK（硬件依赖在 ``main()`` 内），可被离线导入。"""
    script = _load_by_path(SCRIPT_PATH, "verify_gravity_under_test")

    assert callable(script.main)
    assert script.T_FF_LIMIT_NM == gravity.T_FF_LIMIT_NM
    assert callable(script._read_state)  # 现场脚本的读数是核心路径，值得钉住存在
