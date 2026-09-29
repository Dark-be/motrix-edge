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

"""现场标定：重力力矩采样（``t_ff`` 重力前馈的辨识数据来源）。

重力补偿要的是 ``τ_g(q)``——**关节角的函数**，与外力无关。本脚本负责**取数**：让臂停稳在若干
位形上，读「实测关节角 + 实测电机力矩」，正反两遍落盘（拟合在 ``scripts/fit_gravity.py``，见
``wiki/plan/robot_pipeline_impedance_plan.md``）。

三种模式（**默认只读**）::

    python scripts/verify_gravity.py --port left                       # ① 只读：读数是否可用 / 噪声 / 开销
    python scripts/verify_gravity.py --port left --hold                 # ② 符号单位自检（只维持当前位形）
    python scripts/verify_gravity.py --port left --sweep --out /tmp/g.json   # ③ 扫位形采样（会运动）

判读：

- ① 每位关节的 ``|torque|`` 应远小于 ±16 N·m、标准差小；**单次读取耗时**决定 6 次/拍能否放进 30 Hz；
- ② ``τ_meas`` 应与 ``k_p·(q_des − q_meas)`` **同号、同量级**（同一个量从两侧算出来），不一致说明
  读数符号 / 单位 / 关节序与下发不一致——**这一步不过就不能上前馈**（方向错等于主动推机器人）；
- ③ 汇总里的「最大 ``|τ|`` 是否超 ±16 N·m」「每关节行程」「静摩擦幅值」是后续拟合与限幅的依据。

⚠️ ``--sweep`` **会让机械臂运动**：请在**无人、无负载（或记录当前负载）、急停可达**的前提下使用，
并确认点位已按当前限位过滤（越界的一个都不发）。``--hold`` 不下发位移，只持续重发当前位形。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from robot.gravity import (  # noqa: E402
    DEFAULT_SETTLE,
    DEFAULT_WINDOW,
    T_FF_LIMIT_NM,
    SettleDetector,
    friction_from_passes,
    plan_waypoints,
    save_samples,
    summarize_samples,
    window_stats,
)
from robot.kinematics import PiperKinematics  # noqa: E402
from utils.data_handler import debug_print  # noqa: E402

DEFAULT_HZ = 30.0
SETTLE_TIMEOUT_S = 5.0  # 单个位形的判稳上限（超时 → 丢弃该点）
READ_FIELDS = ("position", "velocity", "torque")  # get_motor_states().msg 的字段


def _read_state(controller) -> dict | None:
    """读一次 6 个关节的 ``q`` / ``vel`` / ``tau``（含本次读取耗时 ms）。

    ``get_motor_states`` 是**按关节**的请求 / 应答（1-based），所以这里循环 6 次——「6 次/拍能否放进
    30 Hz」正是 ``--read`` 要量的事情之一。任一关节返回 ``None`` → 本次读数作废（返回 ``None``）。
    """
    started = time.perf_counter()
    q = np.zeros(6, dtype=np.float64)
    vel = np.zeros(6, dtype=np.float64)
    tau = np.zeros(6, dtype=np.float64)
    for index in range(6):
        state = controller.robot.get_motor_states(index + 1)
        if state is None:
            return None
        values = [
            float(np.asarray(getattr(state.msg, field), dtype=np.float64).reshape(-1)[0]) for field in READ_FIELDS
        ]
        q[index], vel[index], tau[index] = values
    return {"q": q, "vel": vel, "tau": tau, "read_ms": (time.perf_counter() - started) * 1e3}


def _send_and_wait_stable(controller, q_target, detector, *, hz, timeout_s=SETTLE_TIMEOUT_S) -> list[dict]:
    """以 ``hz`` 持续重发 ``q_target`` 直到判稳 → 再采一窗样本；超时返回空列表。

    ``move_mit`` 是**直通、无平滑**模式：不重发就等于不通讯，所以判稳前每一拍都要发。``set_joint``
    会**就地修改**传入数组（既有语义），故每次都传 ``copy()``。
    """
    detector.reset()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        controller.set_joint(q_target.copy())
        state = _read_state(controller)
        if state is None:
            debug_print("GRAVITY", "读取电机状态失败，本次读数作废", "ERROR")
            return []
        if detector.update(state["q"], state["vel"], state["tau"]):
            break
        time.sleep(1.0 / hz)
    else:
        return []

    window: list[dict] = []
    for _ in range(DEFAULT_WINDOW):
        controller.set_joint(q_target.copy())
        state = _read_state(controller)
        if state is None:
            return []
        window.append(state)
        time.sleep(1.0 / hz)
    return window


def _mode_read(controller, *, seconds, hz) -> int:
    """只读：不发送任何指令，报告读数统计与单次读取耗时。"""
    samples: list[dict] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = _read_state(controller)
        if state is None:
            print("读取失败（某关节返回 None）", file=sys.stderr)
            return 1
        samples.append(state)
        time.sleep(1.0 / hz)
    if not samples:
        print("没有采到样本", file=sys.stderr)
        return 1

    q = np.stack([item["q"] for item in samples])
    vel = np.stack([item["vel"] for item in samples])
    tau = np.stack([item["tau"] for item in samples])
    read_ms = np.array([item["read_ms"] for item in samples])
    print(f"\n[只读 {len(samples)} 拍]")
    for joint in range(6):
        print(
            f"  J{joint + 1}: q={q[:, joint].mean():+.4f}±{q[:, joint].std():.4f} rad  "
            f"|v|max={np.abs(vel[:, joint]).max():.4f} rad/s  "
            f"tau={tau[:, joint].mean():+.3f}±{tau[:, joint].std():.3f} N·m"
        )
    print(
        f"  读取耗时: {read_ms.min():.2f} / {read_ms.mean():.2f} / {read_ms.max():.2f} ms "
        f"(min/mean/max，6 关节一次)；30 Hz 预算 = 33.3 ms"
    )
    print(f"  |tau|max = {np.abs(tau).max():.3f} N·m（t_ff 限幅 ±{T_FF_LIMIT_NM:g} N·m）")
    return 0


def _mode_hold(controller, kinematics, *, hz) -> int:
    """符号 / 单位自检：以当前实测位形为目标 → 比对 ``τ_meas`` 与 ``k_p·(q_des − q_meas)``。"""
    from robot.controller.piper_controller import MIT_CTRL_CFG  # 现场依赖：仅本机可用

    state = _read_state(controller)
    if state is None:
        print("读取初始位形失败", file=sys.stderr)
        return 1
    target = state["q"].copy()
    window = _send_and_wait_stable(controller, target, SettleDetector(), hz=hz)
    if not window:
        print("未判稳（超时）——检查是否有人 / 外力干扰，或放宽判稳阈值", file=sys.stderr)
        return 1

    stats = window_stats(window)
    kp = np.array([cfg["kp"] for cfg in MIT_CTRL_CFG], dtype=np.float64)
    sag = target - stats["q"]
    predicted = kp * sag
    print(f"\n[符号单位自检] 判稳后窗口 {stats['n']} 拍")
    print("  J   q_des−q_meas(rad)   k_p*(Δq)(N·m)   τ_meas(N·m)   τ_std   同号?")
    agree = 0
    for joint in range(6):
        same_sign = bool(np.sign(predicted[joint]) == np.sign(stats["tau"][joint])) or (
            abs(predicted[joint]) < 0.05 and abs(stats["tau"][joint]) < 0.05
        )
        agree += int(same_sign)
        print(
            f"  {joint + 1}   {sag[joint]:+.4f}            {predicted[joint]:+.3f}         "
            f"{stats['tau'][joint]:+.3f}      {stats['tau_std'][joint]:.3f}   {'✓' if same_sign else '✗'}"
        )
    print(f"  同号关节 {agree}/6；|tau|max={np.abs(stats['tau']).max():.3f} N·m；")
    if agree < 6:
        print("  ⚠️ 存在不同号关节：读数符号 / 关节序 / 单位需要核对，先不要上重力前馈")
        return 1
    return 0


def _mode_sweep(controller, kinematics, args) -> int:
    """扫位形采样（正反两遍）→ 汇总 → 落盘。"""
    state = _read_state(controller)
    if state is None:
        print("读取初始位形失败", file=sys.stderr)
        return 1
    if args.home:
        home = np.asarray(json.loads(args.home), dtype=np.float64).reshape(-1)
        if home.size != 6:
            print("--home 需要 6 个数", file=sys.stderr)
            return 2
    else:
        home = state["q"].copy()

    points, dropped = plan_waypoints(
        home, span=args.span, limits=kinematics.joint_limits, combined=args.combined, seed=args.seed
    )
    print(f"\n[采样] 基准位形: {np.round(home, 4).tolist()}")
    print(
        f"  规划位形 {len(points)} 个（越界丢弃 {len(dropped)} 个: {dropped[:8]}{'...' if len(dropped) > 8 else ''}）"
    )
    print("  ⚠️ 机械臂即将运动：确认无人、急停可达")
    if not points:
        print("没有可用位形（span 太大或基准位形贴近限位）", file=sys.stderr)
        return 2

    samples: list[dict] = []
    dropped_points: list[str] = []
    detector = SettleDetector(DEFAULT_SETTLE)
    try:
        for tag, sequence in (("fwd", points), ("rev", list(reversed(points)))):
            for label, target in sequence:
                window = _send_and_wait_stable(controller, target, detector, hz=args.hz)
                if not window:
                    dropped_points.append(f"{label}[{tag}]")
                    print(f"  · {label}[{tag}] 未判稳 → 丢弃")
                    continue
                stats = window_stats(window)
                samples.append(
                    {
                        "label": label,
                        "direction": tag,
                        "q": stats["q"],
                        "tau": stats["tau"],
                        "tau_std": stats["tau_std"],
                        "vel_absmax": stats["vel_absmax"],
                        "target": target,
                    }
                )
                print(
                    f"  · {label}[{tag}] q={np.round(stats['q'], 4).tolist()} "
                    f"tau={np.round(stats['tau'], 3).tolist()} std={np.round(stats['tau_std'], 4).tolist()}"
                )
    except KeyboardInterrupt:
        print("\n⚠️ 收到中断：已采样本仍会落盘", file=sys.stderr)

    forward = [item for item in samples if item["direction"] == "fwd"]
    backward = [item for item in samples if item["direction"] == "rev"]
    friction = friction_from_passes(forward, backward)
    summary = summarize_samples(samples, friction=friction)
    payload = {
        "version": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "port": args.port,
        "role": args.role,
        "hz": args.hz,
        "span": args.span,
        "payload_note": args.payload,
        "home": home,
        "limits": np.asarray(kinematics.joint_limits, dtype=np.float64),
        "samples": samples,
        "dropped_points": dropped_points,
        "summary": summary,
    }
    out = save_samples(args.out, payload)
    print(f"\n[汇总] 样本 {summary['n']} 条（丢弃 {len(dropped_points)} 点）→ {out}")
    print(
        f"  |tau|max        : {summary['tau_abs_max']:.3f} N·m @ J{summary['tau_abs_max_joint']}"
        f"（限幅 ±{T_FF_LIMIT_NM:g}，余量 {summary['tau_abs_max_headroom']:+.3f}）"
    )
    spread = np.asarray(summary["q_spread"])
    for joint in range(6):
        print(f"  J{joint + 1} 行程     : {spread[0, joint]:+.3f} … {spread[1, joint]:+.3f} rad")
    if friction["paired"]:
        print(
            f"  静摩擦估计      : 均值 {friction['mean']:.3f} / 最大 {friction['max']:.3f} N·m"
            f"（配对 {friction['paired']} 对，逐关节 {np.round(friction['per_joint'], 3).tolist()}）"
        )
    else:
        print("  静摩擦估计      : 无配对样本（正反两遍的位形差异过大）")
    if summary["exceeds_t_ff_limit"]:
        print(f"  ⚠️ 有样本 |tau| 超过 ±{T_FF_LIMIT_NM:g} N·m：前馈会被固件削顶（该位形的静差无法靠重力项消除）")
    return 0 if summary["n"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="重力力矩采样：t_ff 重力前馈的辨识数据来源（现场用）")
    parser.add_argument("--port", default="can0", help="CAN 口名（默认 can0；双臂按现场接线填）")
    parser.add_argument(
        "--role", default="follower", choices=("leader", "follower"), help="默认 follower（--hold/--sweep 必须能从臂）"
    )
    parser.add_argument("--hz", type=float, default=DEFAULT_HZ, help=f"下发 / 采样频率（默认 {DEFAULT_HZ:g} Hz）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--read", action="store_true", help="只读：不发送任何指令（默认）")
    mode.add_argument("--hold", action="store_true", help="符号单位自检：维持当前位形并比对 τ_meas 与 k_p·Δq")
    mode.add_argument("--sweep", action="store_true", help="扫位形采样（⚠️ 会运动）")
    parser.add_argument("--seconds", type=float, default=3.0, help="--read 的采样时长（秒）")
    parser.add_argument("--span", type=float, default=0.3, help="--sweep 每关节摆动幅度（rad）")
    parser.add_argument("--combined", type=int, default=0, help="--sweep 额外采的多关节组合点数")
    parser.add_argument("--seed", type=int, default=0, help="多关节组合的随机种子（可复现）")
    parser.add_argument("--home", default=None, help="--sweep 基准位形（JSON 6 个数；缺省用当前位形）")
    parser.add_argument(
        "--payload", default="unknown", help="负载备注（bare / gripper / 工件名）：决定这份样本的适用范围"
    )
    parser.add_argument("--out", default="gravity_samples.json", help="--sweep 样本输出路径（JSON）")
    args = parser.parse_args()

    from robot.controller.piper_controller import PiperController  # 现场依赖：仅本机可用

    kinematics = PiperKinematics()
    controller = PiperController("gravity")
    controller.connect(port=args.port, role=args.role)
    moving = args.hold or args.sweep
    if moving and args.role == "leader":
        print("⚠️ leader 模式不可由程序驱动：--hold / --sweep 需要 --role follower", file=sys.stderr)
        controller.disconnect()
        return 2

    try:
        if args.hold:
            return _mode_hold(controller, kinematics, hz=args.hz)
        if args.sweep:
            return _mode_sweep(controller, kinematics, args)
        return _mode_read(controller, seconds=args.seconds, hz=args.hz)
    finally:
        if moving:
            state = _read_state(controller)
            if state is not None:
                controller.set_joint(state["q"].copy())  # 回原位（不额外运动）
                time.sleep(0.5)
        controller.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
