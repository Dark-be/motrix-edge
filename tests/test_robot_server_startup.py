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

"""robot server 的启动路径：**导入无副作用**、只构建一次、配置名来源与启动清单。

回归点（都在现场输出里直接可见）：

- 导入 ``server.robot_server`` 不该构建 app：曾经在模块级写 ``app = build_app()``，与 ``main()``
  里按 ``--config`` 的二次构建叠加 → 启动日志 / 采集器初始化各打两遍、配置白读一遍
  （丢弃的那份也已经建过 robot / collector，将来构造函数里接硬件就是双开）；
- 配置名优先级：``--config`` > ``$MOTRIX_ROBOT_PIPELINE_CFG`` > ``test_robot.yml``
  （文档曾写 ``$ROBOT_SERVER_CFG``，代码里并不读——现场会静默起成虚拟机器人）；
- 启动清单给出**实际生效**的配置文件路径与**绝对**落盘目录（相对路径按启动时 cwd 解析）。
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RPH_SRC = _REPO_ROOT / "robot-pipeline" / "src"


@pytest.fixture(scope="module")
def server():
    """导入 robot-pipeline 的 ``server.robot_server``（离线：``test_robot`` 不需要硬件 SDK）。"""
    if str(_RPH_SRC) not in sys.path:
        sys.path.insert(0, str(_RPH_SRC))
    return importlib.import_module("server.robot_server")


def _run_in_rph_src(code: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """在 robot-pipeline/src 的项目口径下跑一段脚本（配置根指向临时目录，防污染仓库）。"""
    env = {
        **os.environ,
        # robot-pipeline/src（config / env / server / robot）与仓库 src（motrix_edge 契约）都要在路径上
        "PYTHONPATH": os.pathsep.join([str(_RPH_SRC), str(_REPO_ROOT / "src")]),
        "MOTRIX_ROBOT_PIPELINE_DIR": str(tmp_path / "root"),
    }
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_RPH_SRC,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_import_builds_nothing(tmp_path):
    """导入即无副作用：不打启动清单、不建 env / app（模块级 ``app`` 已改惰性构建）。"""
    done = _run_in_rph_src("import server.robot_server", tmp_path)

    assert done.returncode == 0, done.stderr
    assert done.stdout == "", f"导入就产生了输出：{done.stdout!r}"


def test_main_builds_app_exactly_once(tmp_path):
    """``python src/server/robot_server.py --config X`` 只构建一次（回归：曾构建两次）。"""
    code = "\n".join(
        [
            "import sys",
            "import server.robot_server as srv",
            "orig = srv.load_config",
            "calls = []",
            "def counting(name, machine=None):",
            "    calls.append(name)",
            "    return orig(name, machine)",
            "srv.load_config = counting",  # 每次 build_app 恰好调用一次
            "srv.serve = lambda app, host=None, port=None: None",  # 不起 uvicorn
            'sys.argv = ["robot_server.py", "--config", "test_robot"]',
            "srv.main()",
            'print("BUILD_COUNT", len(calls), calls)',
        ]
    )
    done = _run_in_rph_src(code, tmp_path)

    assert done.returncode == 0, done.stderr
    assert "BUILD_COUNT 1 ['test_robot']" in done.stdout, done.stdout


def test_app_attribute_is_built_lazily_and_cached(server, monkeypatch):
    """``uvicorn server.robot_server:app``：取属性时才建，且缓存（重复取值不重建）。"""
    monkeypatch.delattr(server, "app", raising=False)
    built = []

    def fake_build(*args, **kwargs):
        built.append(args)
        return SimpleNamespace(state=SimpleNamespace())

    monkeypatch.setattr(server, "build_app", fake_build)

    assert server.app is server.app  # 两次取值同一实例
    assert len(built) == 1
    del server.app  # 惰性构建会缓存回模块命名空间：别把这份假 app 留给同模块其他用例


def test_config_name_precedence(server, monkeypatch):
    """``--config`` > ``$MOTRIX_ROBOT_PIPELINE_CFG`` > ``test_robot.yml``。"""
    seen: list[str] = []
    monkeypatch.setattr(server, "load_config", lambda name, machine=None: (seen.append(name), {"server": {}})[1])
    monkeypatch.setattr(server, "get_env", lambda cfg: object())
    monkeypatch.setattr(
        server, "create_app", lambda env, host=None, port=None: SimpleNamespace(state=SimpleNamespace())
    )

    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_CFG", "dual_piper")
    server.build_app()
    assert seen[-1] == "dual_piper"

    server.build_app("single_piper")
    assert seen[-1] == "single_piper"  # 显式参数优先于环境变量

    monkeypatch.delenv("MOTRIX_ROBOT_PIPELINE_CFG")
    server.build_app()
    assert seen[-1] == "test_robot.yml"  # 兜底：虚拟机器人（无硬件，最安全）


def test_robot_note_lists_wiring(server):
    note = server._robot_note(
        {
            "type": "dual_piper_robot",
            "ports": {"left": "left", "right": "right", "left_master": "m_left", "right_master": "m_right"},
            "cameras": {"cam_head": "1", "cam_left_wrist": "2", "cam_right_wrist": "3"},
            "gravity": {"enabled": True, "alpha": 1.0, "t_ff_limit": 16.0},
        }
    )

    assert note == (
        "dual_piper_robot（DualPiperRobot） | ports 4 路 | cameras 3 路 | gravity α=1.0 / t_ff_limit=16.0 N·m"
    )


def test_robot_note_omits_absent_wiring(server):
    """没有接线 / 前馈的机型（如 test_robot）只报类型与实现类，不留空占位。"""
    assert server._robot_note({"type": "test_robot"}) == "test_robot（TestRobot）"


def test_collector_note_resolves_absolute_save_dir(server, tmp_path, monkeypatch):
    """落盘目录给绝对路径：相对路径按启动时 cwd 解析（docker 里 cwd 与挂载不同，最容易看错）。"""
    monkeypatch.chdir(tmp_path)
    note = server._collector_note(
        {"save_dir": "./data/test_robot", "image_format": "jpeg", "skip_until_motion": {"enabled": True}}
    )

    assert note == (
        f"save_dir={(tmp_path / 'data' / 'test_robot').resolve()} | image_format=jpeg | skip_until_motion=on"
    )


def test_startup_block_reports_effective_paths(server, capsys, tmp_path, monkeypatch):
    """启动清单：config 行给实际读到的文件，各行标签对齐（现场一眼能扫）。"""
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(tmp_path / "root"))
    monkeypatch.delenv("MOTRIX_ROBOT_PIPELINE_MACHINE", raising=False)
    cfg = server.load_config("test_robot")

    server._log_startup("test_robot", None, cfg, "INFO")
    out = capsys.readouterr().out

    assert "robot process server 启动" in out
    assert f"config    test_robot.yml → {tmp_path / 'root' / 'config' / 'test_robot.yml'}" in out
    assert "machine   " in out and "（无档案 → 按机型默认）" in out
    assert "robot     test_robot（TestRobot）" in out
    assert "log       INFO" in out
    assert "collect   save_dir=/" in out  # 绝对路径
