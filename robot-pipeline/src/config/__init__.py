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

"""config 包 —— robot server 路径解析（**一个根目录管 config + logs**）+ 配置加载（包内 yml 是**示例**）。

根目录由环境变量 ``MOTRIX_ROBOT_PIPELINE_DIR`` 指定；**未设置时回落到 ``<cwd>/motrix-robot-pipeline/``**
（与 motrix_edge 同构：那边是 ``MOTRIX_EDGE_DIR`` + ``<cwd>/motrix-edge/``）：:

    <根>/
    ├── config/   实际配置
    │   ├── <机型>.yml            整份机型配置（包内示例**首次访问播种**到此处，之后以它为准）
    │   ├── robot/<机器名>.yml    机器档案：只写「这台机器不同」的键（深合并到机型 yml 上）
    │   └── gravity/*.json        重力参数（标定产物，scripts/fit_gravity.py --install 写入）
    └── logs/     log_<时间戳>.txt（开关 MOTRIX_EDGE_LOG_FILE，与 motrix_edge 共用）

⚠️ **包内 ``src/config/*.yml`` 只是示例**（随包分发、只读、零现场值）：播种是**一次性**的，包内示例
以后更新不会自动覆盖现场副本（想回到示例：删掉 ``<根>/config/<机型>.yml`` 再跑一次）。

- 读配置：:func:`load_config` = 先播种机型示例 → 读 ``<根>/config/<机型>.yml`` → 再**深合并机器档案**
  （``--machine`` / ``MOTRIX_ROBOT_PIPELINE_MACHINE`` / hostname 选中；每台机器差异只写档案，机型 yml 保持零现场值）；
- 写配置：:func:`writable_config_path` = ``<根>/config/...``（机器档案 / 机型覆盖都写这里）；
- **``<cwd>`` 兜底的含义**：从不同目录启动 → 读不同配置 / 写不同日志。现场与容器请显式设
  ``MOTRIX_ROBOT_PIPELINE_DIR``（容器内必须是容器可见路径，否则落容器可写层、重启即丢）。
  生成 / 更新档案：``bash scripts/setup_robot.sh``（用 :mod:`config.probe` 枚举设备、逐个插识别角色）。

本模块在 import 时计算模块级 ``CONFIG_DIR`` / ``LOG_PATH``（环境变量须在进程启动前设置）。
"""

import copy
import os
import socket
from importlib import resources
from pathlib import Path

import yaml

# 环境变量名（单点定义；两个项目的根各用一个变量，均以 ``MOTRIX_`` 开头）。
ENV_ROOT_DIR = "MOTRIX_ROBOT_PIPELINE_DIR"
#: 机型配置名（如 ``dual_piper``）：给脚本/服务一个「默认机型」，省去每次传 ``--config``。
ENV_CONFIG_NAME = "MOTRIX_ROBOT_PIPELINE_CFG"
#: 机器档案名（每台机器一份）：``<根>/config/robot/<machine>.yml``。
ENV_MACHINE = "MOTRIX_ROBOT_PIPELINE_MACHINE"
# 根下的两个子目录（配置与日志同一个根）
CONFIG_DIR_NAME = "config"
LOG_DIR_NAME = "logs"
# 未设环境变量时的根目录名：<cwd>/motrix-robot-pipeline。加 ``motrix-`` 前缀而不是直接叫
# ``robot-pipeline``：避免与仓库里的 ``robot-pipeline/`` 源码目录同名——从仓库根启动时
# 配置 / 日志会被写进源码目录。
ROOT_DIR_NAME = "motrix-robot-pipeline"
#: 机器档案在配置目录下的子目录名（也可被 ``can_muti_activate.sh`` 读到）。
PROFILE_DIR_NAME = "robot"

# robot server 配置示例（package data；**只是示例**——实际配置在 <根>/config，见模块 docstring）
DEFAULT_CONFIG_FILES = (
    "test_robot.yml",
    "dual_piper.yml",
    "dual_alicia_piper.yml",
    "single_piper.yml",
)


def get_root_dir() -> Path:
    """根目录：``$MOTRIX_ROBOT_PIPELINE_DIR``；未设置 → ``<cwd>/motrix-robot-pipeline``（``~`` 展开）。"""
    env = os.getenv(ENV_ROOT_DIR)
    return Path(env).expanduser() if env else Path.cwd() / ROOT_DIR_NAME


def get_config_dir() -> Path:
    """实际配置目录：``<根>/config``（机型 yml / ``robot/<machine>.yml`` / ``gravity/*.json`` 都在这里）。"""
    return get_root_dir() / CONFIG_DIR_NAME


def get_log_dir() -> Path:
    """日志目录：``<根>/logs``。"""
    return get_root_dir() / LOG_DIR_NAME


def config_path(name: str) -> Path:
    """配置文件的**实际路径**：``<根>/config/<name>``（是否存在见 :func:`seed_config`）。"""
    return get_config_dir() / name


def writable_config_path(name: str) -> Path:
    """可写配置路径：与 :func:`config_path` 同（配置目录本身就是可写位置）。"""
    return config_path(name)


def packaged_config_text(name: str) -> str | None:
    """包内示例 yml 的文本；包内没有该文件 → ``None``。"""
    if name not in DEFAULT_CONFIG_FILES:
        return None
    try:
        return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):  # 打包缺失：按「没有示例」处理
        return None


def seed_config(name: str) -> Path:
    """把包内示例 yml **播种**到 ``<根>/config/<name>``（幂等：目标已存在则不动）；返回目标路径。

    包内没有该示例（如运行期产物）→ 只返回路径、不创建文件。写失败（只读挂载 / 无权限）抛
    ``OSError``，由调用方决定降级（:func:`_base_config` 会吞掉并回落示例文本）还是报错。
    """
    target = config_path(name)
    if target.exists():
        return target
    text = packaged_config_text(name)
    if text is None:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    target.chmod(0o644)  # 固定 0644：配置文件不该受 umask（可能 0664）影响
    return target


def list_configs() -> list[str]:
    """可选机型配置名（``<根>/config/*.yml`` 的文件名，排序）；一个都还没有 → 包内示例名。"""
    directory = get_config_dir()
    names = sorted(path.name for path in directory.glob("*.yml")) if directory.is_dir() else []
    return names or list(DEFAULT_CONFIG_FILES)


#: 新建档案时写在文件头的注释（每次写入重新生成，所以档案里不必手改注释）。
PROFILE_HEADER = """\
# 机器档案（每台机器一份）：只写「这台机器不同」的键，其余继承机型 yml（深合并）。
# 常见键：robot.name / robot.ports / robot.cameras / robot.gravity.arms.<臂>.params /
#         collector.save_dir / can.bindings（USB 物理口 -> <目标CAN名>:<波特率>）。
# 生成 / 更新：bash scripts/setup_robot.sh --config <机型> --machine <本机名>
# 生效：robot server 启动时按 --machine / MOTRIX_ROBOT_PIPELINE_MACHINE / hostname 自动叠加。
"""


def machine_path(machine: str) -> Path:
    """机器档案路径：``<根>/config/robot/<machine>.yml``（是否存在由调用方判断）。"""
    return get_config_dir() / PROFILE_DIR_NAME / f"{machine}.yml"


def resolve_machine(explicit: str | None = None) -> str | None:
    """本机机器名：``explicit``（``--machine``）> ``MOTRIX_ROBOT_PIPELINE_MACHINE`` > ``hostname``。"""
    return explicit or os.getenv(ENV_MACHINE) or socket.gethostname() or None


def list_machines() -> list[str]:
    """已有的机器档案名（``<根>/config/robot/*.yml`` 的文件名去后缀，排序）；无 → []。"""
    directory = get_config_dir() / PROFILE_DIR_NAME
    return sorted(path.stem for path in directory.glob("*.yml")) if directory.is_dir() else []


def deep_merge(base: dict, overlay: dict) -> dict:
    """深合并：两沏都是 ``dict`` 时递归合并，其余类型（含 ``list``）**整体替换**。

    所以档案写 ``robot.ports.left`` 只覆盖这一个端口（机型 yml 的其余端口不受影响），而
    ``init_joint`` 这类列表写进档案即整体替换（不会与机型默认逐元素混起来）。
    """
    merged = dict(base or {})
    for key, value in (overlay or {}).items():
        current = merged.get(key)
        merged[key] = (
            deep_merge(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else copy.deepcopy(value)
        )
    return merged


def config_override_path(name: str) -> Path:
    """机型 yml 的实际路径（``<根>/config/<name>``；包内示例由 :func:`seed_config` 播种到这里）。"""
    return config_path(_yml_name(name))


def _yml_name(name: str) -> str:
    """机型配置名补齐 ``.yml`` 后缀：``load_config("dual_piper")`` 与 ``"dual_piper.yml"`` 等价。

    命令行（``--config``）与脚本都允许不写后缀；不补的话会当成另一个文件名，
    :func:`_base_config` 找不到就静默回落 ``{}``（现场表现为「配置全空」很难查）。
    """
    return name if name.endswith(".yml") else f"{name}.yml"


def save_config_override(name: str, patch: dict) -> Path:
    """把 ``patch`` 深合并进**机型 yml**（``<根>/config/<name>``）并写回（读-合并-写；不存在先播种）。

    这份文件就是 robot server 直接读的那份，所以现场值写这里即直接生效；:mod:`config.setup` 的
    ``--dump-bindings`` 也从它取 ``can.bindings``。
    **代价**：一旦存在（播种或手改），包内示例的后续更新不会自动生效——只写差异请用
    :func:`save_machine_profile`。
    """
    path = config_path(_yml_name(name))
    seed_config(_yml_name(name))  # 幂等：没有本地副本就先播种，拿机型默认当底
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    merged = deep_merge(current if isinstance(current, dict) else {}, patch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _override_header(_yml_name(name)) + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _override_header(name: str) -> str:
    """机型 yml 的注释头（每次写入重新生成，所以文件里的注释不必手改）。"""
    return (
        f"# {name}：本机实际配置（由 scripts/setup_robot.sh --target config 生成 / 更新）。\n"
        "# 注意：包内示例的后续更新不会自动生效——只写差异请改用机器档案（robot/<machine>.yml）。\n"
    )


def save_machine_profile(machine: str, patch: dict) -> Path:
    """把 ``patch`` 深合并进机器档案并写回（读-合并-写；目录不存在则创建）；返回档案路径。

    Reads:
        与 :func:`load_config` 同一路径规则（``<根>/config/robot/<machine>.yml``）。
    """
    path = machine_path(machine)
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    merged = deep_merge(current if isinstance(current, dict) else {}, patch)
    merged.setdefault("machine", machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROFILE_HEADER + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def resolve_config_file(name: str) -> Path:
    """配置文件 / 数据文件的真实路径：``<根>/config/<name>`` 存在则用它，否则用包内示例路径。

    用于非 yml 的数据文件（如 ``gravity/piper_6dof.json`` 重力参数；**不播种**——没有本地副本时
    走包内占位参数，缺文件由调用方决定怎么处理）。
    """
    local = config_path(name)
    if local.exists():
        return local
    return Path(str(resources.files(__package__).joinpath(name)))


def effective_config_path(name: str) -> Path:
    """该机型配置**实际生效**的文件路径（``.yml`` 后缀可省）：本地副本优先，否则包内示例。

    与 :func:`load_config` 同一取值口径，供启动清单 / 排障打印真实来源——现场「改了包内示例
    不生效」用路径一比就能看出读的是哪份。
    """
    return resolve_config_file(_yml_name(name))


def _base_config(name: str) -> dict:
    """机型配置本体（不含机器档案）：先播种，再读 ``<根>/config/<name>``；读不到 → 包内示例 / {}。"""
    name = _yml_name(name)
    try:
        path = seed_config(name)
    except OSError:
        path = config_path(name)  # 只读挂载：播种失败就照常尝试读本地副本
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    text = packaged_config_text(name)
    return (yaml.safe_load(text) or {}) if text else {}


def load_config(name: str, machine: str | None = None) -> dict:
    """加载 robot server 配置：机型 yml（``<根>/config/<name>``，首次访问播种示例）+ 机器档案深合并。

    Args:
        name: 机型配置名（``dual_piper`` / ``dual_piper.yml`` 等价，见 :func:`_yml_name`）。
        machine: 机器档案名；``None`` → 按 :func:`resolve_machine` 解析（``MOTRIX_ROBOT_PIPELINE_MACHINE``
            → hostname）。
            **显式传入但档案不存在 → FileNotFoundError**（避免「以为加载了档案」的静默错误）；
            由环境变量 / hostname 解析出的名字没有档案时静默跳过（行为与不传一致）。

    Raises:
        FileNotFoundError: 显式指定的 ``machine`` 没有档案。
        ValueError: 档案不是 YAML 映射。
    """
    cfg = _base_config(name)
    resolved = resolve_machine(machine)
    if not resolved:
        return cfg
    path = machine_path(resolved)
    if not path.exists():
        if machine is not None:
            raise FileNotFoundError(f"机器档案不存在：{path}（先跑 bash scripts/setup_robot.sh 生成）")
        return cfg
    overlay = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(overlay, dict):
        raise ValueError(f"{path}: 机器档案必须是 YAML 映射（顶层键 = 机型 yml 的同名键）")
    return deep_merge(cfg, overlay)


# 对外暴露（模块级：环境已定的路径）
CONFIG_DIR = get_config_dir()
LOG_PATH = get_log_dir()

__all__ = [
    "ENV_ROOT_DIR",
    "ENV_CONFIG_NAME",
    "ENV_MACHINE",
    "ROOT_DIR_NAME",
    "CONFIG_DIR_NAME",
    "LOG_DIR_NAME",
    "DEFAULT_CONFIG_FILES",
    "PROFILE_DIR_NAME",
    "PROFILE_HEADER",
    "get_root_dir",
    "get_config_dir",
    "get_log_dir",
    "config_path",
    "writable_config_path",
    "packaged_config_text",
    "seed_config",
    "list_configs",
    "resolve_config_file",
    "effective_config_path",
    "load_config",
    "machine_path",
    "resolve_machine",
    "list_machines",
    "deep_merge",
    "config_override_path",
    "save_config_override",
    "save_machine_profile",
    "CONFIG_DIR",
    "LOG_PATH",
]
