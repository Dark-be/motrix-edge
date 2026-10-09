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

"""``motrix_edge.geometry.pointing`` 的测试（纯数学，无硬件）。

钉住：① ``look_at`` 的**无 roll** 规范解确实把指定轴指向目标（``x`` / ``z`` 轴闭式可解，``y``
轴在 ``roll = 0`` 时只能指向同高方向 → 报错）；② 末端系增量 → 基座系的换算是**共轭**不是逐项相加
（绕工具 ``z`` 转 ≠ 绕基座 ``z`` 转，除非两者平行）；③ 装配约定（``EgoAxes``）配错时响亮报错。
"""

import numpy as np
import pytest

from motrix_edge.geometry import (
    ROLL_FREE,
    EgoAxes,
    axis_vector,
    base_delta_from_ego,
    base_rotation_delta_from_ego,
    parse_axis,
    pointing_rpy,
    rpy_to_matrix,
    turned_deg,
)


def test_parse_axis_and_axis_vector() -> None:
    assert parse_axis("+z") == (2, 1.0)
    assert parse_axis("-x") == (0, -1.0)
    assert parse_axis("+Z") == (2, 1.0)  # 大小写宽容
    assert np.allclose(axis_vector("-y"), [0.0, -1.0, 0.0])
    for bad in ("z", " Z ", "+w", "++z", "", None, 3):  # 不带符号 / 非法轴 → 响亮报错
        with pytest.raises(ValueError, match="axis spec"):
            parse_axis(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("axis", ["+z", "-z", "+x", "-x"])
def test_pointing_rpy_aims_the_axis_without_roll(axis: str) -> None:
    """任意方向（含与指向轴反平行的退化情形）：解出的姿态 ``roll ≡ 0``，且该轴确实指向目标。"""
    directions = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.0, 0.0, -1.0],
        [0.3, -0.4, 0.5],
        [1.0, 1.0, 1.0],
        [-2.0, 0.5, 0.1],
    ]
    for direction in directions:
        unit = np.asarray(direction, dtype=np.float64)
        unit = unit / np.linalg.norm(unit)
        rpy = pointing_rpy(axis, unit)
        assert rpy[0] == ROLL_FREE
        aimed = rpy_to_matrix(rpy) @ axis_vector(axis)
        assert np.allclose(aimed, unit, atol=1e-9), f"{axis} → {unit}"


def test_pointing_rpy_rejects_y_axis_off_the_horizontal_plane() -> None:
    """``roll = 0`` 时 ``y`` 轴恒在水平面内 → 指向同高方向可以，其余必须报错。"""
    level = pointing_rpy("+y", [1.0, 1.0, 0.0])
    assert np.allclose(rpy_to_matrix(level) @ axis_vector("+y"), [1.0, 1.0, 0.0] / np.sqrt(2.0), atol=1e-9)
    with pytest.raises(ValueError, match="horizontal"):
        pointing_rpy("+y", [0.0, 0.0, 1.0])


def test_pointing_rpy_rejects_nonzero_roll_and_zero_direction() -> None:
    with pytest.raises(ValueError, match="roll-free"):
        pointing_rpy("+z", [0.0, 0.0, 1.0], roll=0.1)
    with pytest.raises(ValueError, match="non-zero"):
        pointing_rpy("+z", [0.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="3 finite"):
        pointing_rpy("+z", [0.0, 1.0])


def test_ego_axes_mapping_and_delta() -> None:
    default = EgoAxes()
    assert default.as_dict() == {"forward": "+z", "left": "+x", "up": "+y"}
    assert np.allclose(default.delta(forward=0.1), [0.0, 0.0, 0.1])
    assert np.allclose(default.delta(left=0.2, up=-0.3), [0.2, -0.3, 0.0])

    custom = EgoAxes.from_mapping({"forward": "+y", "left": "+x", "up": "+z"})
    assert np.allclose(custom.delta(forward=0.1), [0.0, 0.1, 0.0])  # 现场改成 y 轴向前也支持

    with pytest.raises(ValueError, match="three different axes"):
        EgoAxes.from_mapping({"forward": "+x", "left": "+x", "up": "+z"})  # 三轴重了 → 响亮报错
    with pytest.raises(ValueError, match="axis spec"):
        EgoAxes.from_mapping({"forward": "z"})


def test_base_delta_from_ego_uses_the_current_pose() -> None:
    """同一个末端系增量，姿态不同 → 基座系增量不同（这才是「沿末端自身」的意思）。"""
    assert np.allclose(base_delta_from_ego([0.0, 0.0, 0.0], [0.1, 0.0, 0.0]), [0.1, 0.0, 0.0])
    # 绕 z 转 90°：末端 +x 在基座系里是 +y
    assert np.allclose(base_delta_from_ego([0.0, 0.0, np.pi / 2], [0.1, 0.0, 0.0]), [0.0, 0.1, 0.0], atol=1e-9)
    # 绕 y 转 90°：末端 +x 在基座系里是 -z
    assert np.allclose(base_delta_from_ego([0.0, np.pi / 2, 0.0], [0.1, 0.0, 0.0]), [0.0, 0.0, -0.1], atol=1e-9)


def test_base_rotation_delta_from_ego_is_a_chart_increment() -> None:
    """旋转增量给的是**chart 增量**：机器人逐分量相加后 == 绕末端轴的矩阵复合（不是共轭三元组）。

    机器人侧 ``pose_delta`` 的 rpy 是**逐分量相加**（``target_rpy += Δrpy``），所以我们下发的必须
    是 ``wrap(rpy(R·ΔR) - rpy)``；直接下发共轭 ``R·ΔR·Rᵀ`` 的 rpy 会在目标 ``pitch`` 不为 0 时明显
    偏（``pitch=45°`` 绕末端 z 转 20° 偏 10.7°）。
    """

    def applied(rpy, delta_rpy):
        chart = base_rotation_delta_from_ego(rpy, delta_rpy)
        return rpy_to_matrix((np.asarray(rpy) + chart + np.pi) % (2 * np.pi) - np.pi)

    def error(rpy, delta_rpy):
        exact = rpy_to_matrix(rpy) @ rpy_to_matrix(delta_rpy)
        got = applied(rpy, delta_rpy)
        return np.degrees(np.arccos(np.clip((np.trace(exact.T @ got) - 1) / 2, -1, 1)))

    for rpy in ([0.0, 0.0, 0.0], [0.0, 0.0, np.pi / 2], [0.0, np.pi / 2, 0.0], [0.0, np.pi / 4, 0.0]):
        for delta_rpy in ([0.0, 0.0, 0.1], [0.0, 0.0, np.radians(20.0)], [0.05, -0.2, 0.3]):
            assert error(rpy, delta_rpy) < 1e-6, f"rpy={rpy} delta={delta_rpy} 相加后应精确复合"
    # 姿态无旋转：chart 增量退化成逐分量相加（读起来最直观的那种情形）
    assert np.allclose(base_rotation_delta_from_ego([0, 0, 0], [0.0, 0.0, 0.1]), [0.0, 0.0, 0.1], atol=1e-12)
    # 靠近 gimbal lock（pitch = ±90°）：chart 增量会跳到**等价分支**（这里 roll ≈ +π/2）——姿态仍然
    # 精确等价（上面的循环已断言），但数值不再「等于请求的角度」；那种姿态优先用 ``look_at``。
    assert abs(base_rotation_delta_from_ego([0.0, np.pi / 2, 0.0], [0.0, 0.0, 0.1])[0]) > 1.0


def test_turned_deg_measures_pose_difference() -> None:
    assert turned_deg([0, 0, 0], [0, 0, 0]) == pytest.approx(0.0, abs=1e-9)
    assert turned_deg([0, 0, 0], [0, 0, np.pi / 2]) == pytest.approx(90.0, abs=1e-6)
    assert turned_deg([0, 0, np.pi / 2], [0, 0, np.pi / 2 + 0.2]) == pytest.approx(np.degrees(0.2), abs=1e-6)
