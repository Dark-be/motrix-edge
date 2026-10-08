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

"""机器档案（每台机器一份配置）+ 设备探测 + 一键填档（robot-pipeline 侧）。

钉住三件事：

- **叠加语义**：``load_config(name, machine)`` = 机型 yml → **深合并** → ``<根>/config/robot/<machine>.yml``；
  名单（``list``）**整体替换**、映射递归合并；显式指定的档案缺失 → 报错（不静默用机型默认）；
  环境变量 / hostname 解析出的名字没有档案 → 静默跳过（行为与不传一致）；
- **设备类型声明**：每个机器人的 ``PORT_KINDS`` / ``CAMERA_KINDS`` 必须与 ``PORT_ROLES`` /
  ``IMAGE_NAMES`` 完全对齐（一键填档靠它决定「这类设备怎么找」，声明漏了就该报错而不是猜）；
- **探测与补丁**：插拔差分（``added``）识别角色；``patch_for`` 把识别结果翻成档案补丁
  （CAN → 目标名 + ``can.bindings``；串口 / V4L2 → 稳定软链；RealSense → 序列号）。

硬件 SDK 只装在机器人端：与 ``test_state_layout.py`` 同款占位模块。
"""

import importlib
import sys
import types
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"


class _StubMeta(type):
    """占位硬件 SDK 的元类：类属性任取、类调用返回类本身（导入期的 ``BeautyLogger(...)`` 也过）。"""

    def __getattr__(cls, name):
        return cls

    def __call__(cls, *args, **kwargs):
        return cls


class _Stub(metaclass=_StubMeta):
    """占位名字：可调用、可带任意参数、任意属性可读（导入期只要求名字存在）。"""


def _stub_hardware_sdks() -> None:
    """硬件 SDK 只装在机器人端：本机离线测试用占位模块（导入期只需这些名字存在）。"""
    if "pyAgxArm" not in sys.modules:
        sdk = types.ModuleType("pyAgxArm")
        sdk.AgxArmFactory = type("AgxArmFactory", (), {})
        sdk.ArmModel = type("ArmModel", (), {})
        sdk.PiperFW = type("PiperFW", (), {"V188": "V188"})
        sdk.create_agx_arm_config = lambda *args, **kwargs: {}
        sys.modules["pyAgxArm"] = sdk
    if "alicia_d_sdk" not in sys.modules:
        # Alicia 示教臂 SDK：模块级就 `BeautyLogger(log_dir=..., min_level=LogLevel.ERROR)`，故需要可调用占位
        sdk = types.ModuleType("alicia_d_sdk")
        sdk.__path__ = []  # 当成包：允许 `alicia_d_sdk.utils.logger` 子模块
        utils = types.ModuleType("alicia_d_sdk.utils")
        utils.__path__ = []
        logger = types.ModuleType("alicia_d_sdk.utils.logger")
        logger.BeautyLogger = _Stub
        logger.LogLevel = _Stub
        utils.logger = logger
        sdk.utils = utils
        sdk.create_robot = _Stub
        sys.modules["alicia_d_sdk"] = sdk
        sys.modules["alicia_d_sdk.utils"] = utils
        sys.modules["alicia_d_sdk.utils.logger"] = logger
    for name in ("pyrealsense2", "v4l2"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)


def _load(module_name: str):
    """从 robot-pipeline/src 导入模块（离线，占位硬件 SDK）。"""
    _stub_hardware_sdks()
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module(module_name)


@pytest.fixture(scope="module")
def config():
    return _load("config")


@pytest.fixture(scope="module")
def probe():
    return _load("config.probe")


@pytest.fixture(scope="module")
def setup_module():
    return _load("config.setup")


@pytest.fixture
def ext_dir(tmp_path, monkeypatch):
    """**实际配置目录**（``<根>/config``）：机型 yml 与 ``robot/<machine>.yml`` 都放这里。

    根目录由 ``MOTRIX_ROBOT_PIPELINE_DIR`` 指定（未设时回落 ``<cwd>/motrix-robot-pipeline``）。
    """
    root = tmp_path / "root"
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(root))
    monkeypatch.delenv("MOTRIX_ROBOT_PIPELINE_MACHINE", raising=False)
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    return config_dir


# ---- 机器档案：路径 / 叠加语义 ---------------------------------------------------


def test_machine_path_always_under_config_dir(config, tmp_path, monkeypatch):
    """档案路径 = ``<根>/config/robot/<machine>.yml``；根由环境变量决定（未设 → ``<cwd>`` 兜底）。"""
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(tmp_path))
    assert config.machine_path("pc16") == tmp_path / "config" / "robot" / "pc16.yml"

    monkeypatch.delenv("MOTRIX_ROBOT_PIPELINE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config.machine_path("pc16") == tmp_path / "motrix-robot-pipeline" / "config" / "robot" / "pc16.yml"


def test_deep_merge_recurses_maps_and_replaces_lists(config):
    """映射递归合并（只覆盖写到的键）；列表 / 标量整体替换。"""
    base = {"robot": {"ports": {"left": "can_left", "right": "can_right"}, "init_joint": [0, 0, 0]}}
    overlay = {"robot": {"ports": {"left": "m_left"}, "init_joint": [1, 1, 1]}}

    merged = config.deep_merge(base, overlay)

    assert merged["robot"]["ports"] == {"left": "m_left", "right": "can_right"}
    assert merged["robot"]["init_joint"] == [1, 1, 1]
    assert base["robot"]["ports"]["left"] == "can_left"  # 不改原对象


def test_load_config_applies_machine_profile(config, ext_dir):
    """外部目录的同名机型 yml + ``robot/<machine>.yml`` 深合并：只覆盖写到的键。"""
    (ext_dir / "dual_piper.yml").write_text(
        yaml.safe_dump({"robot": {"type": "dual_piper_robot", "ports": {"left": "can_left"}, "step_rad": 0.1}}),
        encoding="utf-8",
    )
    (ext_dir / "robot").mkdir()
    (ext_dir / "robot" / "pc16.yml").write_text(
        yaml.safe_dump({"robot": {"ports": {"left": "m_left", "right": "m_right"}, "name": "dual_piper_pc16"}}),
        encoding="utf-8",
    )

    cfg = config.load_config("dual_piper.yml", "pc16")

    assert cfg["robot"]["ports"] == {"left": "m_left", "right": "m_right"}  # 覆盖
    assert cfg["robot"]["step_rad"] == 0.1  # 机型默认保留
    assert config.list_machines() == ["pc16"]


def test_load_config_without_profile_falls_back_silently(config, ext_dir):
    """环境变量 / hostname 解析出的机器名没有档案 → 按机型默认跑（不报错、不编值）。"""
    (ext_dir / "dual_piper.yml").write_text(yaml.safe_dump({"robot": {"type": "dual_piper_robot"}}), encoding="utf-8")

    assert config.load_config("dual_piper.yml") == {"robot": {"type": "dual_piper_robot"}}


def test_load_config_explicit_machine_must_exist(config, ext_dir):
    """显式 ``--machine`` 但档案不存在 → 报错（避免「以为加载了档案」）。"""
    with pytest.raises(FileNotFoundError, match="机器档案不存在"):
        config.load_config("dual_piper.yml", "pc16")


def test_load_config_seeds_example_and_accepts_name_without_suffix(config, ext_dir):
    """机型名不带 ``.yml`` 也认（补齐后缀）；首次访问把包内**示例**播种到 ``<根>/config``。"""
    assert not (ext_dir / "dual_piper.yml").exists()

    cfg = config.load_config("dual_piper")  # 无后缀

    assert cfg["robot"]["type"] == "dual_piper_robot"  # 内容来自包内示例
    assert (ext_dir / "dual_piper.yml").exists()  # 已播种（幂等，只做一次）
    assert config.config_override_path("dual_piper") == ext_dir / "dual_piper.yml"
    assert config.load_config("dual_piper") == config.load_config("dual_piper.yml")


def test_local_copy_wins_over_packaged_example(config, ext_dir):
    """播种后的本地副本优先于包内示例（现场一改就生效，不用碰包内文件）。"""
    (ext_dir / "test_robot.yml").write_text(
        yaml.safe_dump({"robot": {"type": "test_robot", "name": "现场改过"}}), encoding="utf-8"
    )

    cfg = config.load_config("test_robot.yml")

    assert cfg["robot"]["name"] == "现场改过"


def test_save_machine_profile_merges_and_keeps_existing(config, ext_dir):
    """写档案 = 读-合并-写：已有键保留、补丁覆盖、顶层带上 ``machine``（并把注释头写上）。"""
    config.save_machine_profile("pc16", {"robot": {"name": "dual_piper_pc16"}})
    path = config.save_machine_profile("pc16", {"can": {"bindings": {"3-2.2:1.0": "left:1000000"}}})

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert payload["machine"] == "pc16"
    assert payload["robot"]["name"] == "dual_piper_pc16"  # 上一次写入的保留
    assert payload["can"]["bindings"] == {"3-2.2:1.0": "left:1000000"}
    assert path.read_text(encoding="utf-8").startswith("# 机器档案")


def test_save_machine_profile_uses_cwd_fallback(config, tmp_path, monkeypatch):
    """没设根变量也写得下：根回落到 ``<cwd>/motrix-robot-pipeline``（配置在 ``<cwd>/.../config``）。"""
    monkeypatch.delenv("MOTRIX_ROBOT_PIPELINE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    path = config.save_machine_profile("pc16", {"robot": {"name": "x"}})

    assert path == tmp_path / "motrix-robot-pipeline" / "config" / "robot" / "pc16.yml"


# ---- 设备声明 / 步骤 / 补丁 ------------------------------------------------------


@pytest.mark.parametrize(
    "robot_type",
    ["dual_piper_robot", "dual_alicia_piper_robot", "single_piper_robot", "test_robot"],
)
def test_robot_declares_device_kinds(setup_module, robot_type):
    """每个机器人的 PORT_KINDS / CAMERA_KINDS 与键清单对齐（漏声明由 check_declarations 报错）。

    ``test_robot`` 没有真实接线（无 ``PORT_ROLES``、相机是 ``virtual``）→ 两张表都应为空。
    """
    robot_class = setup_module.load_robot_class(robot_type)

    setup_module.check_declarations(robot_class)  # 不一致会 ValueError
    assert set(robot_class.PORT_KINDS) == set(getattr(robot_class, "PORT_ROLES", ()))
    assert set(robot_class.CAMERA_KINDS) == set(robot_class.IMAGE_NAMES)


def test_check_declarations_rejects_missing_kind(setup_module):
    """漏声明 kind → 报错（而不是猜设备类型：猜错会填错档案）。"""

    class _Robot:
        PORT_ROLES = ("left", "right")
        PORT_KINDS = {"left": "can"}  # 少了 right
        IMAGE_NAMES: list[str] = []
        CAMERA_KINDS: dict[str, str] = {}

    with pytest.raises(ValueError, match="缺 \\['right'\\]"):
        setup_module.check_declarations(_Robot)


def test_steps_skip_virtual_devices(setup_module):
    """``virtual`` 键（测试替身）不需要现场插设备 → 不进步骤表。"""
    robot_class = setup_module.load_robot_class("test_robot")

    assert setup_module.steps_for(robot_class) == []  # test_robot：3 相机全是 virtual

    dual = setup_module.load_robot_class("dual_alicia_piper_robot")
    steps = setup_module.steps_for(dual)

    assert ("ports", "left_master", "serial") in steps and ("cameras", "cam_head", "realsense") in steps
    assert ("cameras", "cam_left_wrist", "v4l2") in steps


def test_patch_for_maps_identity_to_profile(setup_module):
    """识别结果 → 档案补丁：CAN 写目标名 + bindings；串口 / V4L2 写稳定软链；相机写序列号。"""
    can = setup_module.patch_for("ports", "left", "can", "3-2.2:1.0", {"iface": "can0"}, bitrate=1000000)
    serial = setup_module.patch_for("ports", "left_master", "serial", "/dev/serial/by-id/usb-x", None, bitrate=0)
    camera = setup_module.patch_for("cameras", "cam_left_wrist", "v4l2", "/dev/v4l/by-id/usb-y", None, bitrate=0)
    head = setup_module.patch_for("cameras", "cam_head", "realsense", "12362227", None, bitrate=0)

    assert can == {"robot": {"ports": {"left": "left"}}, "can": {"bindings": {"3-2.2:1.0": "left:1000000"}}}
    assert serial == {"robot": {"ports": {"left_master": "/dev/serial/by-id/usb-x"}}}
    assert camera == {"robot": {"cameras": {"cam_left_wrist": "/dev/v4l/by-id/usb-y"}}}
    assert head == {"robot": {"cameras": {"cam_head": "12362227"}}}


def test_merge_patch_accumulates(setup_module):
    """补丁累积：多次识别同一 section 的不同键都要留下。"""
    patch: dict = {}
    setup_module.merge_patch(patch, {"robot": {"ports": {"left": "left"}}, "can": {"bindings": {"p1": "left:1000000"}}})
    setup_module.merge_patch(
        patch, {"robot": {"cameras": {"cam_head": "1"}}, "can": {"bindings": {"p2": "right:1000000"}}}
    )

    assert patch["robot"]["ports"] == {"left": "left"}
    assert patch["robot"]["cameras"] == {"cam_head": "1"}
    assert patch["can"]["bindings"] == {"p1": "left:1000000", "p2": "right:1000000"}


# ---- 档案 → CAN 脚本的绑定表（TSV）--------------------------------------------


def test_dump_bindings_prints_tsv(setup_module, tmp_path, capsys):
    """can 脚本用 ``--dump-bindings`` 读配置（脚本自己不在 sudo 下解析 YAML）。"""
    profile = tmp_path / "pc16.yml"
    profile.write_text(yaml.safe_dump({"can": {"bindings": {"3-2.2:1.0": "left:1000000"}}}), encoding="utf-8")

    assert setup_module.dump_bindings(str(profile)) == 0
    assert capsys.readouterr().out == "3-2.2:1.0\tleft:1000000\n"


def test_dump_bindings_reads_robot_config_by_name(setup_module, ext_dir, capsys):
    """给**机型名** → 走 load_config（外界机型 yml 优先）＝ robot server 读的同一份配置。"""
    (ext_dir / "dual_piper.yml").write_text(
        yaml.safe_dump(
            {
                "robot": {"type": "dual_piper_robot", "ports": {"left": "left", "right": "right"}},
                "can": {"bindings": {"3-2.2:1.0": "left:1000000", "3-2.1:1.0": "right:1000000"}},
            }
        ),
        encoding="utf-8",
    )

    assert setup_module.dump_bindings("dual_piper", "pc16") == 0
    assert capsys.readouterr().out == "3-2.1:1.0\tright:1000000\n3-2.2:1.0\tleft:1000000\n"


def test_dump_bindings_merges_machine_profile(setup_module, ext_dir, capsys):
    """bindings 写在机器档案里也能读（机型 yml 给 ports、档案给 can.bindings → 合并）。"""
    (ext_dir / "dual_piper.yml").write_text(
        yaml.safe_dump({"robot": {"type": "dual_piper_robot", "ports": {"left": "left"}}}), encoding="utf-8"
    )
    (ext_dir / "robot").mkdir()
    (ext_dir / "robot" / "pc16.yml").write_text(
        yaml.safe_dump({"can": {"bindings": {"3-2.2:1.0": "left:1000000"}}}), encoding="utf-8"
    )

    assert setup_module.dump_bindings("dual_piper", "pc16") == 0
    assert capsys.readouterr().out == "3-2.2:1.0\tleft:1000000\n"


def test_dump_bindings_rejects_ports_mismatch(setup_module, ext_dir):
    """bindings 目标名与 robot.ports 对不上 → 报错（否则绑定出的接口名不是 server 要找的名字）。"""
    (ext_dir / "dual_piper.yml").write_text(
        yaml.safe_dump(
            {
                "robot": {"type": "dual_piper_robot", "ports": {"left": "can_left", "right": "right"}},
                "can": {"bindings": {"3-2.2:1.0": "left:1000000", "3-2.1:1.0": "right:1000000"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不一致"):
        setup_module.dump_bindings("dual_piper", "pc16")


def test_dump_bindings_reports_missing(setup_module, tmp_path, ext_dir):
    """文件不存在 / 缺 can.bindings / 无档案 → 明确报错（指向该用的参数与 setup_robot.sh）。"""
    with pytest.raises(FileNotFoundError, match="配置文件不存在"):
        setup_module.dump_bindings(str(tmp_path / "none.yml"))

    profile = tmp_path / "pc16.yml"
    profile.write_text(yaml.safe_dump({"robot": {"name": "x"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="can.bindings"):
        setup_module.dump_bindings(str(profile))

    with pytest.raises(FileNotFoundError, match="setup_robot.sh"):
        setup_module.dump_bindings(None, "pc16")


def test_save_config_override_materializes_and_merges(config, ext_dir):
    """``--target config``：外界机型 yml 不存在则**物化**当前配置，再写盘；二次写入保留已有键。"""
    path = config.save_config_override("dual_piper.yml", {"robot": {"name": "dual_piper_pc16"}})
    materialized = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert path == ext_dir / "dual_piper.yml"
    assert materialized["robot"]["type"] == "dual_piper_robot"  # 包内示例被物化（否则这台机器没配置可跑）
    assert materialized["robot"]["name"] == "dual_piper_pc16"
    assert "can" not in materialized

    config.save_config_override("dual_piper.yml", {"can": {"bindings": {"3-2.2:1.0": "left:1000000"}}})
    merged = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert merged["robot"]["name"] == "dual_piper_pc16"  # 保留
    assert merged["can"]["bindings"] == {"3-2.2:1.0": "left:1000000"}
    assert path.read_text(encoding="utf-8").startswith("# dual_piper.yml：本机实际配置")


# ---- 探测：kind / 差分 / 稳定标识 -----------------------------------------------


def test_probe_virtual_is_empty_and_unknown_kind_rejected(probe):
    """``virtual``（测试替身）恒空；未知 kind 直接报错（不静默返回空）。"""
    assert probe.probe("virtual") == []

    with pytest.raises(ValueError, match="unknown device kind"):
        probe.probe("usb-camera")


def test_added_only_returns_new_identities(probe):
    """插拔差分：只回新增设备（识别角色靠它，故必须是稳定标识）。"""
    assert probe.added({"a", "b"}, {"a", "b", "c"}) == ["c"]
    assert probe.added({"a"}, {"a"}) == []


def test_identity_prefers_stable_keys(probe):
    """稳定标识：RealSense → 序列号；CAN → bus-info；V4L2 / 串口 → 软链路径。"""
    assert probe.identity("realsense", {"serial": "123", "name": "D435"}) == "123"
    assert probe.identity("can", {"iface": "can0", "bus_info": "3-2.2:1.0"}) == "3-2.2:1.0"
    assert probe.identity("v4l2", "/dev/v4l/by-id/usb-x") == "/dev/v4l/by-id/usb-x"


def test_list_v4l2_prefers_by_id_and_index0(probe, tmp_path):
    """一个 UVC 相机会暴露 index0/1/2：只取主采集节点 ``video-index0``。"""
    root = tmp_path / "by-id"
    root.mkdir()
    for name in ("usb-cam-a-video-index0", "usb-cam-a-video-index1", "usb-cam-b-video-index0"):
        (root / name).write_text("", encoding="utf-8")

    assert probe.list_v4l2(root) == [str(root / "usb-cam-a-video-index0"), str(root / "usb-cam-b-video-index0")]


def test_probe_map_drops_items_without_identity(probe, monkeypatch):
    """没有稳定标识的设备（如取不到 bus-info 的 CAN 口）不进对比表。"""
    monkeypatch.setattr(
        probe, "list_can", lambda: [{"iface": "can0", "bus_info": ""}, {"iface": "can1", "bus_info": "3-2.1:1.0"}]
    )

    assert list(probe.probe_map("can")) == ["3-2.1:1.0"]


def test_list_can_parses_ip_and_ethtool(probe, monkeypatch):
    """CAN 枚举 = ``ip -br link show type can`` + ``ethtool -i``（与 can 激活脚本同一判据）。"""

    def fake_run(*args):
        if args[:4] == ("ip", "-br", "link", "show") and args[-1] == "can":
            return "can1  UP  <NOARP,ECHO>\ncan0  DOWN  <NOARP,ECHO>\n"
        if args[:2] == ("ethtool", "-i"):
            return f"bus-info: 3-2.{args[2][-1]}:1.0\n"
        if args[:3] == ("ip", "-details", "link"):
            return (
                f"{args[3]}: <NOARP,ECHO> mtu 16\n    can state ERROR-ACTIVE (berr-counter tx 0 rx 0) bitrate 1000000\n"
            )
        return "UP\n"

    monkeypatch.setattr(probe, "_run", fake_run)
    entries = probe.list_can()

    assert [entry["iface"] for entry in entries] == ["can0", "can1"]  # 按 bus-info 排序
    assert entries[1]["bus_info"] == "3-2.1:1.0" and entries[1]["bitrate"] == "1000000" and entries[1]["up"] is True
