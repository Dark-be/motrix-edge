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

"""机器档案一键生成 / 更新：**交互式逐个插设备**，把「这台机器实际插了什么」写进档案。

入口是 ``scripts/setup_robot.sh``（薄封装：定位仓库 / 选 python / 透传参数），本模块是实际逻辑；
也可以直接跑::

    PYTHONPATH=src python -m config.setup --machine pc16            # 交互式逐个插
    PYTHONPATH=src python -m config.setup --machine pc16 --list     # 只看现状（不改档）
    PYTHONPATH=src python -m config.setup --machine pc16 --dry-run  # 只打印将写入的补丁

为什么是「逐个插」而不是「按序列号排序自动填」：**角色 ↔ 设备**的对应只有人能提供（哪台相机是头部、
哪个 CAN 口是左从臂）。逐个插的差分（:func:`config.probe.added`）让人不必知道 bus-info / 序列号，
也不会出现「左右腕颠倒」这类数据里极难发现的错。

写进档案的键（只写「这台机器不同」的键，其余继承机型 yml）：

- ``robot.ports.<角色>``：CAN 角色 → **绑定后的接口名**（= 角色名，与 ``can.bindings`` 一致）；
  串口角色 → ``/dev/serial/by-id/*`` 稳定软链；
- ``robot.cameras.<相机名>``：RealSense → 序列号；V4L2 → ``/dev/v4l/by-id/*`` 稳定软链；
- ``can.bindings``：``{USB 物理口(bus-info): "<角色>:<波特率>"}``，由 ``can_muti_activate.sh`` 读取；
- ``robot.name`` / ``collector.save_dir``：展示名与每机数据目录（可回车保持机型默认）。

设备类型来自机器人类的 ``PORT_KINDS`` / ``CAMERA_KINDS`` 声明（见 :mod:`config.probe` 的 kind）；
声明缺失 → 直接报错要求补（**不猜**）。
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

import yaml

from config import (
    ENV_CONFIG_NAME,
    ENV_MACHINE,
    ENV_ROOT_DIR,
    config_override_path,
    get_config_dir,
    list_configs,
    machine_path,
    resolve_machine,
    save_config_override,
    save_machine_profile,
)
from config.probe import (
    CAN_BITRATE_DEFAULT,
    KIND_CAN,
    KIND_VIRTUAL,
    KINDS,
    ProbeUnavailable,
    added,
    probe_map,
)

#: 交互输入的保留字。
ANSWER_SKIP = "s"
ANSWER_QUIT = "q"


def steps_for(robot_class) -> list[tuple[str, str, str]]:
    """需要现场探测的步骤：``[(section, key, kind), ...]``（``virtual`` 键跳过，无需插设备）。

    section ∈ ``ports`` / ``cameras``；键顺序 = 机器人类的声明顺序（现场按这个顺序插即可）。
    没有硬件接线的机器人（如 ``test_robot``：无 ``PORT_ROLES``）自然得到空步骤表。
    """
    steps = []
    for section, keys, kinds in _declarations(robot_class):
        steps.extend((section, key, kinds[key]) for key in keys if kinds[key] != KIND_VIRTUAL)
    return steps


def _declarations(robot_class) -> list[tuple[str, tuple, dict]]:
    """三个声明节：``[(section, 键清单, kind 表), ...]``（缺声明 → 空，不报 KeyError）。"""
    return [
        (
            "ports",
            tuple(getattr(robot_class, "PORT_ROLES", ()) or ()),
            dict(getattr(robot_class, "PORT_KINDS", {}) or {}),
        ),
        (
            "cameras",
            tuple(getattr(robot_class, "IMAGE_NAMES", ()) or ()),
            dict(getattr(robot_class, "CAMERA_KINDS", {}) or {}),
        ),
    ]


def check_declarations(robot_class) -> None:
    """校验 ``PORT_KINDS`` / ``CAMERA_KINDS`` 与键清单、kind 取值一致。

    Raises:
        ValueError: 漏声明 / 多声明 / kind 不在 :data:`config.probe.KINDS` 中。
    """
    for section, keys, kinds in _declarations(robot_class):
        label = f"{robot_class.__name__}.{section.upper()}_KINDS"
        missing = sorted(set(keys) - set(kinds))
        extra = sorted(set(kinds) - set(keys))
        if missing or extra:
            raise ValueError(f"{label} 与键清单不一致：缺 {missing} / 多 {extra}（补声明后重试）")
        bad = sorted({kind for kind in kinds.values() if kind not in KINDS})
        if bad:
            raise ValueError(f"{label} 含未知 kind {bad}（可用：{list(KINDS)}）")


def patch_for(section: str, key: str, kind: str, ident: str, item, *, bitrate: int) -> dict:
    """把「刚识别到的一个设备」翻译成档案补丁（纯函数，便于离线测试）。

    - ``can``：``robot.ports.<角色>`` = 角色名（绑定后的接口名）、``can.bindings[bus-info]`` =
      ``"<角色>:<波特率>"``；
    - ``serial`` / ``v4l2``：写**稳定软链路径**（换 USB 口不变）；
    - ``realsense``：写序列号。
    """
    if kind == KIND_CAN:
        return {
            "robot": {"ports": {key: key}},
            "can": {"bindings": {ident: f"{key}:{int(bitrate)}"}},
        }
    if section == "ports":
        return {"robot": {"ports": {key: ident}}}
    return {"robot": {"cameras": {key: ident}}}


def merge_patch(target: dict, fragment: dict) -> dict:
    """把补丁片段按 ``{顶层: {二级: {键: 值}}}`` 三层合并进累积补丁（列表不出现在补丁里）。"""
    for top, section in fragment.items():
        for name, values in section.items():
            target.setdefault(top, {}).setdefault(name, {}).update(values)
    return target


def describe(kind: str, ident: str, item) -> str:
    """给操作员看的一行说明（RealSense 带上型号 / 物理口，CAN 带上当前接口名）。"""
    if kind == "realsense" and isinstance(item, dict):
        return f"{ident}（{item.get('name') or 'RealSense'} @ {item.get('physical_port') or '?'}）"
    if kind == KIND_CAN and isinstance(item, dict):
        return f"{item.get('bus_info')}（当前 {item.get('iface')}）"
    return ident


def list_current(robot_class, machine: str, path: Path, config_name: str) -> int:
    """``--list``：打印配置路径 / 现有档案 / 各 kind 的当前设备（只读，不改档）。"""
    print(f"机型配置：{config_name}\n机器名：{machine}（根目录 {ENV_ROOT_DIR} 或 <cwd>/motrix-robot-pipeline）")
    print(f"写入位置：{path}")
    if path.exists():
        print("\n--- 现有内容 ---")
        print(path.read_text(encoding="utf-8").rstrip())
        print("--- 内容结束 ---")
    print("\n当前现场设备（按稳定标识）：")
    for kind in KINDS:
        if kind == KIND_VIRTUAL:
            continue
        try:
            found = probe_map(kind)
        except ProbeUnavailable as exc:
            print(f"  {kind:10s}: 不可用（{exc}）")
            continue
        print(f"  {kind:10s}: {list(found) if found else '（无）'}")
    print(
        f"\n待探测步骤（{len(steps_for(robot_class))} 个）：{[(key, kind) for _, key, kind in steps_for(robot_class)]}"
    )
    return 0


def load_robot_class(robot_type: str):
    """按 ``ROBOT_REGISTRY`` 取机器人类（只 import，不实例化——构造需要接线配置，会自锁）。"""
    from robot import ROBOT_REGISTRY  # 惰性：本模块会连带 import 该机器人的 controller（硬件 SDK）

    if robot_type not in ROBOT_REGISTRY:
        raise ValueError(f"robot.type={robot_type!r} 未注册（可选：{list(ROBOT_REGISTRY)}）")
    module_path, class_name = ROBOT_REGISTRY[robot_type]
    return getattr(importlib.import_module(module_path), class_name)


def choose_config(preset: str | None) -> str:
    """机型配置名：``--config`` 优先；否则列出 ``<根>/config/*.yml`` 交互选择（dual_piper 优先）。

    本地还没有 yml 时（尚未播种）列的是**包内示例**——打印时如实标注，避免误以为
    ``<根>/config`` 里已经有这些文件。
    """
    if preset:
        return preset if preset.endswith(".yml") else f"{preset}.yml"
    directory = get_config_dir()
    names = list_configs()
    where = directory if any(directory.glob("*.yml")) else f"包内示例，将播种到 {directory}"
    print(f"可选机型配置（{where}）：")
    for index, name in enumerate(names, 1):
        print(f"  {index}) {name}")
    preferred = "dual_piper.yml" if "dual_piper.yml" in names else names[0]
    answer = input(f"选择（1-{len(names)}，回车 = {preferred}）: ").strip()
    if not answer:
        return preferred
    if not answer.isdigit() or not 1 <= int(answer) <= len(names):
        raise ValueError(f"无效选择：{answer!r}")
    return names[int(answer) - 1]


def ask(prompt: str, default: str) -> str:
    """带缺省值的一行输入（回车 → 缺省）。"""
    answer = input(f"{prompt}（回车保持 {default!r}）: ").strip()
    return answer or default


def collect(robot_class, *, bitrate: int) -> dict:
    """交互式逐个插设备 → 累积档案补丁（``q`` 放弃、``s`` 跳过）。"""
    steps = steps_for(robot_class)
    patch: dict = {}
    state: dict[str, dict] = {}
    print(f"\n共 {len(steps)} 步：每步**把该设备插上**（其余保持不动），插好后回车。")
    print(f"输入 {ANSWER_SKIP} 跳过该步、{ANSWER_QUIT} 放弃（不写档案）。\n")
    for index, (section, key, kind) in enumerate(steps, 1):
        baseline = state.get(kind) or probe_map(kind)  # 首次以当前现场为基线
        while True:
            answer = input(f"[{index}/{len(steps)}] 插上「{key}」（{kind}）后回车: ").strip().lower()
            if answer == ANSWER_QUIT:
                raise KeyboardInterrupt
            if answer == ANSWER_SKIP:
                print(f"  · 跳过 {key}")
                break
            after = probe_map(kind)
            state[kind] = after
            fresh = added(baseline, after)
            if len(fresh) != 1:
                detail = "、".join(fresh) if fresh else "无"
                print(f"  ⚠️ 检测到 {len(fresh)} 个新设备（{detail}）：一次只插**一个**，或输 {ANSWER_SKIP} 跳过")
                baseline = after
                continue
            ident = fresh[0]
            print(f"  ✅ {key} → {describe(kind, ident, after[ident])}")
            merge_patch(patch, patch_for(section, key, kind, ident, after[ident], bitrate=bitrate))
            baseline = after
            break
    return patch


def _read_bindings(payload: dict, source: str) -> dict:
    """从配置里取 ``can.bindings``（缺 / 非映射 → ValueError）。"""
    can_section = payload.get("can") if isinstance(payload, dict) else None
    bindings = can_section.get("bindings") if isinstance(can_section, dict) else None
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError(
            f'{source}: 缺 can.bindings（形如 can: {{bindings: {{"3-2.2:1.0": "left:1000000"}}}}；'
            "用 bash scripts/setup_robot.sh 生成）"
        )
    return {str(port): str(value) for port, value in bindings.items()}


def _profile_arg(machine: str | None) -> str | None:
    """只有当机器档案**确实存在**时才把机器名交给 load_config。

    否则：显式机器名 + 无档案会让 ``load_config`` 报错（那是给 robot server 用的行为），而这里
    「现场值全写在机型 yml 里」的用法本就不需要档案——不能因此跑不了。
    """
    resolved = resolve_machine(machine)
    if not resolved:
        return None
    return resolved if machine_path(resolved).exists() else None


def _resolve_bindings_payload(target: str | None, machine: str | None) -> tuple[dict, str]:
    """取「含 ``can.bindings`` 的配置」→ ``(payload, 来源说明)``。

    - ``target`` 已是**文件**（或形状像 yml 路径）→ 直接读该文件（不叠加，便于手改 / 归档）；
    - ``target`` 否则当**机型名** → :func:`config.load_config`（``<根>/config/<机型>.yml`` + 机器档案叠加），
      **与 robot server 读的是同一份合并结果**；
    - ``target`` 为空 → 先用环境变量 ``MOTRIX_ROBOT_PIPELINE_CFG`` 当机型名，仍为空才用机器档案
      ``<根>/config/robot/<machine>.yml``。
    """
    from config import load_config

    if not target:
        target = os.getenv(ENV_CONFIG_NAME, "").strip() or None
    if target:
        candidate = Path(target).expanduser()
        if candidate.exists():
            if not candidate.is_file():
                raise ValueError(f"{candidate} 不是文件")
            return yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}, str(candidate)
        if candidate.suffix in (".yml", ".yaml") or "/" in target:
            raise FileNotFoundError(f"配置文件不存在：{candidate}")
        name = target if target.endswith(".yml") else f"{target}.yml"
        return load_config(name, _profile_arg(machine)), f"{name}（实际配置目录 + 机器档案叠加）"

    resolved = resolve_machine(machine)
    path = machine_path(resolved) if resolved else None
    if path is None or not path.exists():
        raise FileNotFoundError(
            f"机器档案不存在：{path}；可改用 --config <机型名>（读实际配置目录里的机型 yml），"
            "或先跑 bash scripts/setup_robot.sh 生成档案"
        )
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}, str(path)


def _check_can_ports(payload: dict, bindings: dict) -> list[str]:
    """校对 ``can.bindings`` 的目标名 ↔ ``robot.ports`` 里 **can** 角色的值（best-effort）。

    绑定出来的接口名就是 robot server 要找的名字，两边对不上现场必然连不上——所以宁可在这里
    报错。校对需要机器人类的 ``PORT_KINDS`` 声明；缺硬件 SDK / 未注册机型时只返回空（不阻断）。
    """
    robot_section = payload.get("robot") if isinstance(payload, dict) else None
    robot_type = (robot_section or {}).get("type") if isinstance(robot_section, dict) else None
    if not robot_type:
        return []
    try:
        robot_class = load_robot_class(str(robot_type))
    except (ValueError, ImportError) as exc:
        print(f"[WARN]: 跳过 robot.ports 校对（导入 {robot_type} 失败：{exc}）", file=sys.stderr)
        return []
    kinds = dict(getattr(robot_class, "PORT_KINDS", {}) or {})
    ports = dict((robot_section or {}).get("ports") or {})
    can_ports = {key: str(ports.get(key, "")) for key, kind in kinds.items() if kind == KIND_CAN}
    targets = {value.split(":", 1)[0] for value in bindings.values()}
    # 空值不在这里管（那是 robot server 的「ports 必填」校验）：只校对「两边都写了」的部分。
    problems = [
        f"robot.ports.{key} = {value!r} 不在 can.bindings 的目标名里（{sorted(targets)}）"
        for key, value in sorted(can_ports.items())
        if value and value not in targets
    ]
    filled = {value for value in can_ports.values() if value}
    problems += [f"can.bindings 的目标名 {target!r} 没有对应的 robot.ports 键" for target in sorted(targets - filled)]
    return problems


def dump_bindings(target: str | None = None, machine: str | None = None) -> int:
    """把**配置里的** ``can.bindings`` 打成 TSV（``bus-info<TAB>目标名:波特率``）供 can 脚本读。

    ``target`` = 机型名（默认路径：外界机型 yml 优先 + 机器档案叠加 = robot server 同一份配置）
    或 yml 路径；为空 → 机器档案。取到后做一次 ``robot.ports`` 一致性校对（见 :func:`_check_can_ports`）。

    Raises:
        FileNotFoundError: 目标文件 / 档案不存在。
        ValueError: 缺 ``can.bindings``，或目标名与 ``robot.ports`` 不一致。
    """
    payload, source = _resolve_bindings_payload(target, machine)
    bindings = _read_bindings(payload, source)
    problems = _check_can_ports(payload, bindings)
    if problems:
        raise ValueError(f"{source}: can.bindings 与 robot.ports 不一致：\n  - " + "\n  - ".join(problems))
    for port, value in sorted(bindings.items()):  # 稳定顺序：便于对比与日志
        print(f"{port}\t{value}")
    return 0


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    """入口：解析参数 → 交互填档 → 写 ``<根>/config/robot/<machine>.yml``（或 ``--target config`` 写机型 yml）。

    ``root`` = 仓库根（默认当前目录）；测试可传临时目录。
    """
    parser = argparse.ArgumentParser(
        prog="setup_robot",
        description="机器档案一键生成 / 更新（交互式逐个插设备；通常经 scripts/setup_robot.sh 调用）",
    )
    parser.add_argument("--config", default=None, help="机型配置名（如 dual_piper；缺省交互选择）")
    parser.add_argument("--machine", default=None, help=f"机器名（机器档案名）；缺省 ${ENV_MACHINE} 或 hostname")
    parser.add_argument(
        "--target",
        choices=("profile", "config"),
        default="profile",
        help="写哪里：profile（默认）= <根>/config/robot/<machine>.yml（只写差异）；config = <根>/config/<机型>.yml"
        "（整份机器配置，与 robot server 直接读的那份相同；代价是包内示例的后续更新不再自动生效）",
    )
    parser.add_argument("--list", action="store_true", help="只打印现状（不改档案）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将写入的补丁（不改档案）")
    parser.add_argument("--bitrate", type=int, default=CAN_BITRATE_DEFAULT, help="CAN 波特率（缺省 1 Mbps）")
    parser.add_argument(
        "--dump-bindings",
        metavar="CONFIG|FILE",
        nargs="?",
        const="",
        default=None,
        help="只把配置里的 can.bindings 打成 TSV（bus-info\\t目标名:波特率）；取值 = 机型名（走 load_config，"
        "与 robot server 同一份）或 yml 路径；不给值 → 机器档案。由 can_muti_activate.sh 调用",
    )
    args = parser.parse_args(argv)

    if args.dump_bindings is not None:
        return dump_bindings(args.dump_bindings or args.config or None, args.machine)

    config_name = choose_config(args.config or os.getenv(ENV_CONFIG_NAME, "").strip() or None)
    from config import load_config

    cfg = load_config(config_name)  # 机型默认（不含档案，避免把旧档案当默认值）
    robot_type = str((cfg.get("robot") or {}).get("type") or "")
    if not robot_type:
        raise ValueError(f"{config_name} 缺少 robot.type")
    robot_class = load_robot_class(robot_type)
    check_declarations(robot_class)

    machine = resolve_machine(args.machine)
    target_path = config_override_path(config_name) if args.target == "config" else machine_path(machine or "")
    if args.target == "profile" and not machine:
        raise ValueError("无法确定机器名：用 --machine <名> 指定（或改用 --target config）")
    if args.list:
        return list_current(robot_class, machine or "-", target_path, config_name)

    print(f"机型配置：{config_name}（{robot_type}）")
    print(f"机器名：{machine or '-'}｜写入：{target_path}（--target {args.target}）")
    patch = collect(robot_class, bitrate=args.bitrate)
    patch.setdefault("robot", {})["name"] = ask(
        "robot.name（展示名 / discover 上报名）", (cfg.get("robot") or {}).get("name") or robot_class.NAME
    )
    patch.setdefault("collector", {})["save_dir"] = ask(
        "collector.save_dir（每台机器分开存）", (cfg.get("collector") or {}).get("save_dir") or "./data"
    )
    print("\n将写入档案的补丁：")
    print(yaml.safe_dump(patch, allow_unicode=True, sort_keys=False).rstrip())
    if args.dry_run:
        print("\n（--dry-run：未写入）")
        return 0
    written = (
        save_config_override(config_name, patch) if args.target == "config" else save_machine_profile(machine, patch)
    )
    print(f"\n✅ 已写入：{written}")
    if args.target == "config":
        print("注意：这是整份机型配置（播种后的本地副本）——包内示例的后续更新不再自动生效；")
        print("      只写差异请用 --target profile")
    print("生效：robot server 启动时读 <根>/config（按 --machine / MOTRIX_ROBOT_PIPELINE_MACHINE / hostname 叠加档案）")
    print(f"CAN 绑定：sudo bash scripts/can_muti_activate.sh --config {config_name}")
    return 0


def _entry() -> int:
    """命令行入口：把预期错误翻成一行提示（不打印 traceback），``Ctrl+C`` 视为放弃。"""
    try:
        return main()
    except KeyboardInterrupt:
        print("\n已放弃：档案未改动", file=sys.stderr)
        return 130
    except ImportError as exc:
        print(f"❌ [ERROR]: 导入机器人类失败（缺硬件 SDK？在机器人端安装对应依赖后重试）：{exc}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError, ProbeUnavailable) as exc:
        print(f"❌ [ERROR]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_entry())
