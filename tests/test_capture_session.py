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

"""CaptureSession 测试 —— 采集会话（无回合流程控制）。

进入会话后持续消费命令（session quit 退出、robot estop 急停、robot execute / teleop、
capture episode / sync）；**显示观测由节点级写入 frame_manager，会话不再 observe /
写 frame_manager**。用进程内 FakeRobotAdapter（内存态），不启动机器人 SDK 进程 / 网络 /
共享内存。
"""

from fake_robot import FakeRobotAdapter

from motrix_edge.frame import FrameManager
from motrix_edge.session import capture_session
from motrix_edge.session.base import RunResult
from motrix_edge.utils.commands import build_command_registry

_REGISTRY = build_command_registry()


def make_signals(*seq):
    """命令词（空格分隔，不用点）→ Command（经注册表解析，与 CLI 一致），耗尽后返回 None。"""
    import shlex

    it = iter(seq)

    def source():
        cmd = next(it, None)
        if cmd is None:
            return None
        return _REGISTRY.parse_argv(shlex.split(cmd)) if isinstance(cmd, str) else cmd

    return source


def make_config(tmp_path):
    return {
        "adapter": [
            {
                "name": "Test Robot",
                "type": "test_robot",
                "data_dir": str(tmp_path),  # 运行时行为参数（适配器特有）；能力由适配器返回
            }
        ],
        "capture": {"obs_freq": 30},
    }


def test_capture_consumes_commands_until_exit(tmp_path):
    """采集会话：消费命令直到 session quit 退出；**不写 frame_manager**（显示观测归节点级）。"""
    fm = FrameManager()
    session = capture_session.CaptureSession(
        make_config(tmp_path),
        command_source=make_signals("session quit"),
        frame_manager=fm,
        adapter=FakeRobotAdapter(config={"data_dir": str(tmp_path)}),
    )
    session.session_start()
    assert session.run() == RunResult.FINISHED
    session.session_finish()
    # 会话不再 observe / 写 frame_manager（观测由节点级持续写入）
    assert fm.latest() is None


def test_capture_estop_during_observe_safe_stops(tmp_path):
    """观测期间急停：safe_stop 后回 ERROR。"""
    session = capture_session.CaptureSession(
        make_config(tmp_path),
        command_source=make_signals("robot estop"),
        adapter=FakeRobotAdapter(config={"data_dir": str(tmp_path)}),
    )
    session.session_start()
    assert session.run() == RunResult.ERROR
    assert session.state == capture_session.SessionState.ERROR
    session.session_finish()


def test_capture_robot_execute_during_observe(tmp_path):
    """观测期间 robot execute：解析 qpos 参数 → adapter.execute（qpos 直接作为参数）。"""
    adapter = FakeRobotAdapter(config={"data_dir": str(tmp_path)})
    session = capture_session.CaptureSession(
        make_config(tmp_path),
        command_source=make_signals("robot execute 0,0,0,0,0,0,0", "session quit"),
        frame_manager=FrameManager(),
        adapter=adapter,
    )
    session.session_start()
    assert session.run() == RunResult.FINISHED
    assert adapter.executed == [[0.0] * 7]  # qpos 直接作为参数传给 adapter.execute
    session.session_finish()


def test_capture_robot_reset_during_observe(tmp_path):
    """观测期间 robot reset：调用 adapter.reset 并回执 ok（会话继续，不复位会话）。"""
    adapter = FakeRobotAdapter(config={"data_dir": str(tmp_path)})
    replies = []
    reset = _REGISTRY.parse_argv(["robot", "reset"])
    reset.reply_to = replies.append
    session = capture_session.CaptureSession(
        make_config(tmp_path),
        command_source=make_signals(reset, "session quit"),
        frame_manager=FrameManager(),
        adapter=adapter,
    )
    session.session_start()
    assert session.run() == RunResult.FINISHED
    assert adapter.reset_calls >= 2  # run() 开头复位 + 命令复位
    assert replies[0].status == "ok"  # 会话内 robot reset 有回执（不阻塞 submit）
    session.session_finish()


def test_capture_infer_endpoint_during_session(tmp_path):
    """会话内 infer ip set：写内存态 policy.host 并回执 ok（配置命令任何状态可用）。"""
    adapter = FakeRobotAdapter(config={"data_dir": str(tmp_path)})
    replies = []
    infer_ip = _REGISTRY.parse_argv(["infer", "ip", "set", "1.2.3.4"])
    infer_ip.reply_to = replies.append
    cfg = make_config(tmp_path)
    session = capture_session.CaptureSession(
        cfg,
        command_source=make_signals(infer_ip, "session quit"),
        frame_manager=FrameManager(),
        adapter=adapter,
    )
    session.session_start()
    assert session.run() == RunResult.FINISHED
    assert cfg["policy"]["host"] == "1.2.3.4"  # 配置已写内存态 policy 段
    assert replies[0].status == "ok"
    session.session_finish()


def test_capture_stop_returns_error(tmp_path):
    """外部请求停止（stop）：会话主循环立即返回 ERROR（node 失联 ERROR 时终止任务线程）。"""
    session = capture_session.CaptureSession(
        make_config(tmp_path),
        command_source=lambda: None,
        frame_manager=FrameManager(),
        adapter=FakeRobotAdapter(config={"data_dir": str(tmp_path)}),
    )
    session.session_start()
    session.stop()
    assert session.run() == RunResult.ERROR
    assert session.state == capture_session.SessionState.ERROR
    session.session_finish()


def test_capture_robot_teleop_during_observe(tmp_path):
    """观测期间 robot teleop：解析 true/false 参数 → adapter.set_teleop（遥操作开关）。"""
    adapter = FakeRobotAdapter(config={"data_dir": str(tmp_path)})
    session = capture_session.CaptureSession(
        make_config(tmp_path),
        command_source=make_signals("robot teleop true", "robot teleop false", "session quit"),
        frame_manager=FrameManager(),
        adapter=adapter,
    )
    session.session_start()
    assert session.run() == RunResult.FINISHED
    assert adapter.teleop_values == [True, False]  # true → false 逐次设置
    session.session_finish()


def test_capture_episode_start_end_during_observe(tmp_path):
    """观测期间 capture episode start / end：adapter.start_capture / end_capture。"""
    adapter = FakeRobotAdapter(config={"data_dir": str(tmp_path)})
    session = capture_session.CaptureSession(
        make_config(tmp_path),
        command_source=make_signals("capture episode start", "capture episode end", "session quit"),
        frame_manager=FrameManager(),
        adapter=adapter,
    )
    session.session_start()
    assert session.run() == RunResult.FINISHED
    assert adapter.capture_episodes == ["start", "end"]  # 开启一轮 → 结束一轮
    session.session_finish()


def test_capture_sync_meta_during_observe(tmp_path):
    """观测期间 capture sync --meta <json>：解析 JSON → adapter.sync_capture_meta。"""
    adapter = FakeRobotAdapter(config={"data_dir": str(tmp_path)})
    session = capture_session.CaptureSession(
        make_config(tmp_path),
        command_source=make_signals(
            'capture sync --meta \'{"operator": "张三", "task_name": "巡检"}\'',
            "session quit",
        ),
        frame_manager=FrameManager(),
        adapter=adapter,
    )
    session.session_start()
    assert session.run() == RunResult.FINISHED
    assert adapter.capture_meta == {"operator": "张三", "task_name": "巡检"}  # 元信息已同步到进程
    session.session_finish()


def test_capture_sync_rejects_invalid_meta(tmp_path):
    """capture sync 非法 meta JSON → rejected（400），不崩溃、不污染进程元信息。"""
    adapter = FakeRobotAdapter(config={"data_dir": str(tmp_path)})
    replies = []
    cmd = _REGISTRY.parse_argv(["capture", "sync", "--meta", "not-json"])
    cmd.reply_to = replies.append
    session = capture_session.CaptureSession(
        make_config(tmp_path),
        command_source=make_signals(cmd, "session quit"),
        frame_manager=FrameManager(),
        adapter=adapter,
    )
    session.session_start()
    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert adapter.capture_meta == {}
    session.session_finish()
