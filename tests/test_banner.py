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

"""启动概览（``utils/banner``）：卡片内容 / 列对齐 / 一行摘要 / tty 判定。

钉住三件事：**卡片只在终端打**（管道走摘要行）、**配置路径相对根显示**（可读）、
**列按显示宽度对齐**（CJK 双宽，不按 ``len()``）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from motrix_edge.utils.banner import display_width, pad, show_card, startup_card, startup_summary

_FIELDS = {
    "version": "0.1.0",
    "root": "/root/workspace/motrix-edge",
    "config_source": "/root/workspace/motrix-edge/config/edge.yml",
    "log_level": "INFO",
    "file_logging": False,
    "web_url": "http://0.0.0.0:8000",
    "adapter_target": "http://127.0.0.1:8090",
}


def test_card_shows_root_relative_config_and_services():
    """卡片给出根目录、**相对根**的配置短路径、服务与机器人地址。"""
    card = startup_card(**_FIELDS)

    assert "motrix-edge 0.1.0" in card
    assert pad("根目录", 11) + "/root/workspace/motrix-edge" in card
    assert pad("配置", 11) + "config/edge.yml" in card  # 相对根，不打印一长串绝对路径
    assert "http://0.0.0.0:8000" in card
    assert "http://127.0.0.1:8090" in card


def test_card_keeps_config_path_outside_root():
    """``run --config <外部文件>``（根外）原样显示——相对化只会得到一串 ``../``。"""
    card = startup_card(**{**_FIELDS, "config_source": "/etc/motrix-edge/edge.yml"})

    assert "/etc/motrix-edge/edge.yml" in card


def test_card_marks_file_logging_state():
    """文件日志开关在卡片里可见（关闭时明说，避免以为日志写在别处）。"""
    off = startup_card(**_FIELDS)
    on = startup_card(**{**_FIELDS, "file_logging": True})

    assert "logs/（文件日志关闭）" in off
    assert "文件日志关闭" not in on


def test_card_lists_command_cheatsheet():
    """卡片附带最常用的命令（不必先 help 才知道怎么用）。"""
    card = startup_card(**_FIELDS)

    for command in ("session run capture", "session run infer", "session quit", "node reset", "robot estop", "help"):
        assert command in card


def test_second_column_aligns_by_display_width():
    """两列命令行的第二组命令从同一显示列开始（CJK 双宽下也齐）。

    行格式＝4 缩进 + 21 命令列 + 15 说明列 → 第二组命令在显示第 40 列开始；
    注意不能用 ``line[:40]``（那是**字符**切片，说明列是中文时会多切）。
    """
    lines = startup_card(**_FIELDS).splitlines()
    paired = [line for line in lines if any(second in line for second in ("session quit", "node reset", "robot estop"))]

    assert len(paired) == 3
    for line in paired:
        second = next(second for second in ("session quit", "node reset", "robot estop") if second in line)
        assert display_width(line[: line.index(second)]) == 40, line


def test_card_fits_in_80_columns():
    """卡片每行的显示宽度 ≤ 78：常见 80 列终端不折行（折行会把命令列表打乱）。"""
    widths = {display_width(line): line for line in startup_card(**_FIELDS).splitlines()}

    assert max(widths) <= 78, widths[max(widths)]


def test_display_width_counts_cjk_as_two():
    """显示宽度：CJK 算 2 列（``len()`` 会把 ``日志级别`` 算成 4 → 串列）。"""
    assert display_width("日志级别") == 8
    assert display_width("INFO") == 4


def test_pad_pads_by_display_width_and_never_truncates():
    assert display_width(pad("日志", 11)) == 11
    assert pad("已超出宽度", 2) == "已超出宽度"


def test_summary_is_single_line_with_key_values():
    """摘要行：单行、``key=value``（可 grep / 归档），与卡片同一份信息。"""
    line = startup_summary(**_FIELDS)

    assert "\n" not in line
    assert "root=/root/workspace/motrix-edge" in line
    assert "config=/root/workspace/motrix-edge/config/edge.yml" in line
    assert "file_logging=off" in line


def test_show_card_follows_tty(monkeypatch):
    """卡片只在终端打：重定向 / 管道 / systemd 一律走摘要行。"""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    assert show_card() is True

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert show_card() is False


def test_relative_config_display_uses_path_arithmetic():
    """相对化用 ``Path.relative_to``（不靠字符串前缀匹配，避免 ``…/motrix-edge-2`` 误判）。"""
    fields = {**_FIELDS, "root": "/repo", "config_source": "/repo-2/config/edge.yml"}
    card = startup_card(**fields)

    assert "/repo-2/config/edge.yml" in card
    assert Path("/repo-2/config/edge.yml").is_relative_to(Path("/repo")) is False  # 与实现同一判据
