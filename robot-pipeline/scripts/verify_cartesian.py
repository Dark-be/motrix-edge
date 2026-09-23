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

"""现场标定 / 校验：把 ``robot.kinematics`` 的 FK 与真机 SDK 法兰位姿对照。

机器人侧的位姿观测与笛卡尔 IK 都用 ``robot.kinematics`` 的同一套 Modified DH 运动学
（**不读** ``get_flange_pose()``）；因此 DH 参数、关节零位偏移、关节读数符号必须与真机一致
——本脚本就是查这件事的：

1. 读当前关节角 ``q``（SDK）与法兰位姿 ``get_flange_pose()``（SDK）；
2. 用 ``fk(q)`` 算同一姿态的位姿，比较**位置误差（米）**与**姿态误差（弧度，``log3``）**；
3. 若 ``--cycles > 0``，小幅摆动若干关节后重复（默认不动，只读当前位形）；
4. 用 SDK 位姿当目标做一次 IK 往返，报告收敛情况（解算耗时也一并打印）。

判读：位置误差应在毫米级（< 5 mm）、姿态误差在毫弧度级；明显偏大说明 DH / 关节零位 /
关节方向需要修正（改 ``robot/kinematics/piper.py`` 的 ``PIPER_DH`` 与 ``PIPER_JOINT_LIMITS``
——限位是解算与下发共用的**唯一一份**，不经配置覆盖）。

用法（在机器人本机、robot-pipeline 环境内）::

    python scripts/verify_cartesian.py --port left              # 只读对照（推荐先跑）
    python scripts/verify_cartesian.py --port left --role follower
    python scripts/verify_cartesian.py --port left --cycles 3   # 小幅摆动后重复对照

⚠️ 默认**不运动**（``role=leader`` 只读）；``--cycles`` 会经控制器小幅摆动关节
（``↕ 0.1 rad``），请在**无人、无负载、急停可达**的前提下使用。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from robot.kinematics import PiperKinematics, log3, solve_ik  # noqa: E402
from utils.base.data_handler import debug_print  # noqa: E402

_POS_TOL_M = 0.005  # 位置误差可接受上限（5mm）
_ROT_TOL_RAD = 0.02  # 姿态误差可接受上限（约 1.1°）
_WIGGLE_RAD = 0.1  # --cycles 时每个关节的摆动幅度


def _pose_error(expected: np.ndarray, actual: np.ndarray) -> tuple[float, float]:
    """``[xyz, rpy]`` 两个位姿的（位置误差 m，姿态误差 rad）。"""
    pos_err = float(np.linalg.norm(np.asarray(expected[:3]) - np.asarray(actual[:3])))
    rot_err = float(
        np.linalg.norm(
            log3(PiperKinematics.pose_matrix(expected)[:3, :3] @ PiperKinematics.pose_matrix(actual)[:3, :3].T)
        )
    )
    return pos_err, rot_err


def _flange_pose(controller) -> np.ndarray | None:
    """直接读 **SDK** 法兰位姿（标定专用）。

    ``PiperController`` 只管控制链路（关节读写 + 夹爪），**不暴露位姿读写**——所以这里越过它
    直接调 SDK 原生的 ``get_flange_pose()``（只本脚本用，不影响运行时依赖）。
    """
    pose = controller.robot.get_flange_pose()
    if pose is None:
        return None
    return np.asarray(pose.msg, dtype=np.float64).reshape(-1)[:6]


def _sample(controller, kinematics: PiperKinematics) -> dict | None:
    """读一次关节 + 法兰位姿，做 FK 对照与 IK 往返。"""
    joints = controller.get_joint()
    flange = _flange_pose(controller)
    if joints is None or flange is None:
        debug_print("VERIFY", "读取失败（关节或位姿返回 None）", "ERROR")
        return None
    q = np.asarray(joints, dtype=np.float64).reshape(-1)[:6]
    sdk_pose = np.asarray(flange, dtype=np.float64).reshape(-1)[:6]
    fk_pose = kinematics.pose(q)
    pos_err, rot_err = _pose_error(fk_pose, sdk_pose)

    started = time.perf_counter()
    result = solve_ik(kinematics, sdk_pose, q)
    elapsed_ms = (time.perf_counter() - started) * 1e3
    rt_pos_err = rt_rot_err = float("nan")
    if result.ok and result.q is not None:
        rt_pos_err, rt_rot_err = _pose_error(kinematics.pose(result.q), sdk_pose)

    return {
        "q": q,
        "sdk_pose": sdk_pose,
        "fk_pose": fk_pose,
        "pos_err": pos_err,
        "rot_err": rot_err,
        "limit_margin": kinematics.clamp_margin(q),
        "ik_ok": result.ok,
        "ik_reason": result.reason,
        "ik_iters": result.iterations,
        "ik_ms": elapsed_ms,
        "ik_pos_err": rt_pos_err,
        "ik_rot_err": rt_rot_err,
    }


def _print_report(name: str, report: dict) -> None:
    ok = report["pos_err"] <= _POS_TOL_M and report["rot_err"] <= _ROT_TOL_RAD
    verdict = "OK" if ok else "MISMATCH（检查 DH / 关节零位 / 关节方向）"
    print(f"\n[{name}] {verdict}")
    print(f"  q(rad)          : {np.round(report['q'], 4).tolist()}")
    print(f"  SDK 法兰位姿    : {np.round(report['sdk_pose'], 4).tolist()}")
    print(f"  FK 位姿         : {np.round(report['fk_pose'], 4).tolist()}")
    print(f"  位置误差        : {report['pos_err'] * 1000:.2f} mm  (阈值 {_POS_TOL_M * 1000:.0f} mm)")
    print(f"  姿态误差        : {report['rot_err']:.5f} rad  (阈值 {_ROT_TOL_RAD:.3f} rad)")
    print(f"  限位余量        : {report['limit_margin']:.4f} rad")
    print(
        f"  IK 往返         : ok={report['ik_ok']} iters={report['ik_iters']} "
        f"耗时={report['ik_ms']:.2f} ms pos_err={report['ik_pos_err'] * 1000:.3f} mm "
        f"rot_err={report['ik_rot_err']:.5f} rad {('reason=' + report['ik_reason']) if report['ik_reason'] else ''}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="FK 与真机法兰位姿对照 + IK 往返（现场标定用）")
    parser.add_argument("--port", default="can0", help="CAN 口名（默认 can0；双臂机器人按现场接线填）")
    parser.add_argument(
        "--role",
        default="leader",
        choices=("leader", "follower"),
        help="leader = 只读（默认，不上力）；follower = 使能从臂（会 enable）",
    )
    parser.add_argument("--cycles", type=int, default=0, help="摆动轮数（>0 会小幅运动，默认 0 = 只读）")
    parser.add_argument("--wiggle", type=float, default=_WIGGLE_RAD, help="每轮关节摆动幅度（rad）")
    args = parser.parse_args()

    from robot.controller.piper_controller import PiperController  # 现场依赖：仅本机可用

    kinematics = PiperKinematics()
    controller = PiperController("verify")
    controller.connect(port=args.port, role=args.role)
    if args.cycles > 0 and args.role == "leader":
        print("⚠️ leader 模式不可由程序驱动：--cycles > 0 需要 --role follower", file=sys.stderr)
        controller.disconnect()
        return 2

    failures = 0
    try:
        base = controller.get_joint()
        if base is None:
            print("读取初始关节失败", file=sys.stderr)
            return 2
        for cycle in range(max(1, args.cycles)):
            if cycle > 0 and args.cycles > 0:
                q_cmd = np.asarray(base, dtype=np.float64).reshape(-1)[:6].copy()
                q_cmd[cycle % 6] += args.wiggle if cycle % 2 else -args.wiggle
                controller.set_joint(q_cmd)
                time.sleep(1.0)  # 等限速环走完
            report = _sample(controller, kinematics)
            if report is None:
                failures += 1
                continue
            _print_report(f"{args.port} cycle {cycle}", report)
            if report["pos_err"] > _POS_TOL_M or report["rot_err"] > _ROT_TOL_RAD:
                failures += 1
    finally:
        if args.cycles > 0:
            controller.set_joint(np.asarray(base, dtype=np.float64).reshape(-1)[:6])  # 回到起始位形
            time.sleep(1.0)
        controller.disconnect()

    print(f"\n结论：{'全部通过' if failures == 0 else f'{failures} 项超差（见上）'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
