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

"""标定第 2 步：把采样样本拟合为重力参数 ``π̂``（**不碰硬件**，任意机器上都能跑）。

输入是 ``scripts/verify_gravity.py --sweep`` 落盘的样本 JSON（实测 ``q`` + 实测 ``tau``），输出是
控制器读的那份参数文件（``config/gravity/*.json``）。用法::

    python scripts/fit_gravity.py --samples gravity_samples.json                 # 报告 + 写 gravity_params.json
    python scripts/fit_gravity.py --samples gravity_samples.json --ridge 1e-3    # 病态（激励不足）时加岭正则
    python scripts/fit_gravity.py --samples gravity_samples.json --install       # 直接覆盖控制器读的参数文件
    python scripts/fit_gravity.py --self-test                                   # 离线自检（不碰硬件、不要 pytest）

判读（这三条决定这次标定**能不能用**）：

- **残差 RMS** 应落在摩擦量级以下（否则采样里有被碰 / 未停稳的点，看下面「残差最大的位形」）；
- **条件数** 大 / **秩** 不满 → 位形激励不足（多关节组合点太少）：加岭正则只能抑制，真正的办法是
  补几组多关节位形重采；
- 样本里的**最大 ``|τ̂_g|``** 若接近 ±16 N·m，现场那台机器在该位形会被固件削顶，前馈限幅要留意。

⚠️ 参数只在**标定时的负载状态**下成立：换了工件（或拆装过夹爪）必须重标，否则前馈本身就是错的。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config import resolve_config_file  # noqa: E402
from robot.gravity import (  # noqa: E402
    DEFAULT_PARAMS_RELATIVE,
    GRAVITY,
    PI_PER_LINK,
    T_FF_LIMIT_NM,
    GravityCompensator,
    GravityModel,
    fit_gravity,
    gravity_regressor,
    load_samples,
    save_gravity_params,
)
from robot.kinematics import PiperKinematics  # noqa: E402

WORST_SAMPLES = 5  # 报告里列出残差最大的几个位形（现场据此判断哪个点被碰过 / 没停稳）
DOF = 6


def _load_sample_list(path: Path) -> tuple[list[dict], dict]:
    """读样本 JSON → ``(samples, 元信息)``；结构不对直接报错（不要拿半份数据去拟合）。"""
    payload = load_samples(path)
    if not isinstance(payload, dict) or "samples" not in payload:
        raise ValueError(f"{path}: 不是采样脚本的产物（缺 samples 字段）")
    samples = payload["samples"]
    if not samples:
        raise ValueError(f"{path}: samples 为空——采样时是不是全部位形都未判稳？")
    meta = {key: value for key, value in payload.items() if key != "samples"}
    return samples, meta


def _per_sample_residual(samples: list[dict], model, kinematics=None) -> np.ndarray:
    """逐位形残差（``|Y(q)π̂ − τ|`` 的最大分量）——报告里按大小排序找坏点。"""
    residual = []
    for item in samples:
        q = np.asarray(item["q"], dtype=np.float64).reshape(-1)
        tau = np.asarray(item["tau"], dtype=np.float64).reshape(-1)
        residual.append(float(np.max(np.abs(gravity_regressor(q, kinematics) @ model.params.reshape(-1) - tau))))
    return np.asarray(residual)


# ---- 离线自检（不碰硬件；代替单测：改 DH / 改数学后先跑它）-------------------------------


def _potential(q, mass, com, kinematics) -> float:
    """重力势能 ``U = −Σ m_k·gᵀ·p_k``（``p_k`` = 连杆坐标系原点 + ``R_k·c_k``）。"""
    transforms = kinematics.prefix_transforms(np.asarray(q, dtype=np.float64))
    gravity_vector = np.array([0.0, 0.0, -GRAVITY])
    total = 0.0
    for link in range(len(mass)):
        transform = transforms[link + 1]
        center = transform[:3, 3] + transform[:3, :3] @ np.asarray(com[link], dtype=np.float64)
        total -= mass[link] * float(gravity_vector @ center)
    return total


def _self_check(kinematics=None) -> list[tuple[str, bool, str]]:
    """本脚本的数学自检（返回 ``[(检查项, 是否通过, 说明), ...]``）——没通过就别拿它标定。

    钉四件事：① 回归器 = 势能 ``U(q)`` 的数值偏导（结构 / 符号 / 单位）；② 法兰点质量与
    ``PiperKinematics.jacobian`` 独立对齐；③ 拟合能重现合成样本的力矩；④ 限幅 / 占位 / 异常降级。
    """
    kinematics = PiperKinematics() if kinematics is None else kinematics
    rng = np.random.default_rng(0)
    checks: list[tuple[str, bool, str]] = []

    mass = rng.uniform(0.2, 2.0, size=DOF)
    com = rng.uniform(-0.12, 0.12, size=(DOF, 3))
    params = np.concatenate([mass.reshape(-1, 1), mass.reshape(-1, 1) * com], axis=1)
    q = rng.uniform(-0.6, 0.6, size=DOF)

    analytic = gravity_regressor(q, kinematics) @ params.reshape(-1)
    step = 1e-6
    numeric = np.zeros(DOF)
    for joint in range(DOF):
        delta = np.zeros(DOF)
        delta[joint] = step
        numeric[joint] = (
            _potential(q + delta, mass, com, kinematics) - _potential(q - delta, mass, com, kinematics)
        ) / (2 * step)
    checks.append(
        (
            "回归器 = ∂U/∂q（数值偏导）",
            bool(np.allclose(analytic, numeric, atol=1e-6)),
            f"max|Δ|={np.max(np.abs(analytic - numeric)):.1e}",
        )
    )

    point_mass = np.zeros((DOF, PI_PER_LINK))
    point_mass[DOF - 1, 0] = 1.7
    expected = -(kinematics.jacobian(q)[:3].T @ (1.7 * np.array([0.0, 0.0, -GRAVITY])))
    got = gravity_regressor(q, kinematics) @ point_mass.reshape(-1)
    checks.append(
        (
            "法兰点质量 = −J_vᵀ·m·g（含符号）",
            bool(np.allclose(got, expected)),
            f"max|Δ|={np.max(np.abs(got - expected)):.1e}",
        )
    )

    samples = []
    for index in range(24):
        sample_q = rng.uniform(-0.8, 0.8, size=DOF)
        sample_tau = gravity_regressor(sample_q, kinematics) @ params.reshape(-1)
        samples.append({"label": f"s{index}", "q": sample_q, "tau": sample_tau})
    fit = fit_gravity(samples, kinematics=kinematics)
    checks.append(
        (
            "拟合重现合成样本（残差 RMS ≈ 0）",
            fit.residual_rms < 1e-9,
            f"RMS={fit.residual_rms:.1e}，秩 {fit.rank}/{fit.n_params}，条件数 {fit.cond:.3g}",
        )
    )

    model = GravityModel(params, kinematics=kinematics)
    placeholder = GravityCompensator(GravityModel(np.zeros((DOF, PI_PER_LINK)), kinematics=kinematics))
    scaled = GravityCompensator(model, alpha=0.5)
    heavy_params = np.concatenate([np.full((DOF, 1), 40.0), np.full((DOF, 3), 0.3)], axis=1)
    limited = GravityCompensator(GravityModel(heavy_params, kinematics=kinematics), limit=2.0)
    placeholder_ok = bool(np.allclose(placeholder.torque(q), 0.0)) and not placeholder.active
    checks.append(("占位参数 → t_ff 恒 0", placeholder_ok, "active=False"))
    checks.append(("α 线性缩放", bool(np.allclose(scaled.torque(q), 0.5 * model.torque(q))), "α=0.5"))
    checks.append(
        (
            "限幅生效（不超 ±limit）",
            bool(np.max(np.abs(limited.torque(q))) <= 2.0 + 1e-12) and limited.clips >= 1,
            f"limit={limited.limit:g} N·m",
        )
    )
    degraded = scaled.torque([float("nan")] * DOF)
    checks.append(("异常读数 → 本拍 0", bool(np.allclose(degraded, 0.0)), scaled.last_note or ""))
    return checks


def _run_self_check() -> int:
    """跑 :func:`_self_check` 并打印结果；任一失败 → 返回 1（这时候先别拿它标定 / 上前馈）。"""
    print("\n[自检] 重力数学 / 限幅 / 降级（离线，不碰硬件、不需要 pytest）")
    checks = _self_check()
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}：{detail}")
    failed = [name for name, ok, _ in checks if not ok]
    if failed:
        print(f"  自检失败：{failed}")
        return 1
    print(f"  全部通过（{len(checks)} 项）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="重力参数拟合：样本 JSON → π̂（标定产物）")
    parser.add_argument("--samples", default=None, help="--sweep 采出来的样本 JSON")
    parser.add_argument("--out", default="gravity_params.json", help="参数输出路径（默认当前目录）")
    parser.add_argument(
        "--install", action="store_true", help=f"写到控制器实际读的参数文件（{DEFAULT_PARAMS_RELATIVE}），不用再手动拷"
    )
    parser.add_argument("--name", default=DEFAULT_PARAMS_RELATIVE, help="--install 时的相对配置目录路径")
    parser.add_argument("--ridge", type=float, default=0.0, help="岭正则系数（0 = 普通最小二乘；病态时用 1e-3 量级）")
    parser.add_argument("--robot", default="", help="机型 / 机器人名（写进参数文件，便于归档）")
    parser.add_argument(
        "--self-test", action="store_true", dest="self_test", help="只跑离线自检（回归器 / 拟合 / 限幅），不读样本"
    )
    args = parser.parse_args()

    if args.self_test:
        return _run_self_check()
    if not args.samples:
        parser.error("需要 --samples <样本 JSON>（或 --self-test）")

    samples, meta = _load_sample_list(Path(args.samples))
    fit = fit_gravity(samples, ridge=args.ridge)
    residual = _per_sample_residual(samples, fit.model)
    predicted = []
    for item in samples:
        q = np.asarray(item["q"], dtype=np.float64).reshape(-1)
        predicted.append(gravity_regressor(q) @ fit.model.params.reshape(-1))
    predicted = np.stack(predicted)

    print(f"\n[拟合] {args.samples}（负载备注：{meta.get('payload_note', '—')}）")
    print(fit.report())
    print(f"  样本内 |τ̂_g|max = {float(np.max(np.abs(predicted))):.3f} N·m（t_ff 限幅 ±{T_FF_LIMIT_NM:g} N·m）")
    if float(np.max(np.abs(predicted))) > T_FF_LIMIT_NM:
        print(f"  ⚠️ 有被拟合的位形超过 ±{T_FF_LIMIT_NM:g} N·m：现场前馈会在那里被固件削顶")

    worst = np.argsort(residual)[::-1][:WORST_SAMPLES]
    print(f"  残差最大的 {len(worst)} 个位形（被碰 / 未停稳 / 负载变过的点看这里）：")
    for index in worst:
        item = samples[index]
        print(
            f"    {item.get('label', f'#{index}')}[{item.get('direction', '?')}] "
            f"|r|max={residual[index]:.4f} N·m  tau_std_max={np.max(np.asarray(item.get('tau_std', [0.0]))):.4f}"
        )

    target = resolve_config_file(args.name) if args.install else Path(args.out)
    save_gravity_params(
        target,
        fit.model,
        extra={
            "robot": args.robot,
            "payload": meta.get("payload_note"),
            "source_samples": str(args.samples),
            "source_hz": meta.get("hz"),
            "fit": {
                "n_samples": fit.n_samples,
                "n_equations": fit.n_equations,
                "n_params": fit.n_params,
                "rank": fit.rank,
                "cond": fit.cond,
                "ridge": fit.ridge,
                "residual_rms": fit.residual_rms,
                "residual_max": fit.residual_max,
                "residual_rms_per_joint": fit.residual_rms_per_joint.tolist(),
                "torque_abs_max": float(np.max(np.abs(predicted))),
                "report": fit.report(),
            },
        },
    )
    print(f"\n[产物] {target}")
    print(f"  {fit.model.describe()}")
    if not args.install:
        print(f"  提示：用 --install 直接写控制器读的参数文件（{resolve_config_file(args.name)}）")
    else:
        print("  已写控制器读的参数文件：下次启动即生效；现场首跑建议先 α=0.2 逐档验收")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
