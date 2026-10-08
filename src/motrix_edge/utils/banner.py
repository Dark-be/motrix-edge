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

"""启动概览：终端**卡片**（给人看）+ 一行**摘要**（给日志 / 机器看）。

两条互补规则（同一份信息不在同一处刷两遍）：

- **终端**（``stdout`` 是 tty）：打印卡片——路径 / 服务地址 / 常用命令。用 ``print`` 而不是
  ``debug_print``：它是 UI，不该被日志级别过滤，也不该每行都挂 ``[INFO][EdgeNode]`` 前缀；
- **非终端**（systemd / ``nohup`` / 管道 / 只看 ``docker logs``）**或**开启了文件日志：改打
  （补打）一行摘要，走 ``debug_print``——这些场景要的是可 grep / 可归档的日志行，卡片只是噪音。

卡片按**显示宽度**（CJK 双宽）对齐，不按 ``len()``：否则中英混排会把列串开。
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

# 卡片列宽（**显示宽度**，不是字符数）：标签列 / 命令列 / 说明列 / 分隔线。
# 总宽要能落在常用的 80 列终端内：4 缩进 + 21 + 15 + 21 + 最长说明（“复位 / ERROR 恢复” = 17）= 78。
_LABEL_WIDTH = 11
_COMMAND_WIDTH = 21
_DESCRIPTION_WIDTH = 15
_RULE_WIDTH = 48

#: 命令速查（左：命令 / 说明；右：同行第二条命令 / 说明；右键为空表示该行只有一条）
_COMMANDS: tuple[tuple[str, str, str, str], ...] = (
    ("session run capture", "启动采集", "session quit", "退出会话"),
    ("session run infer", "启动推理", "node reset", "复位 / ERROR 恢复"),
    ("robot reset", "机器人复位", "robot estop", "急停"),
    ("help", "全部命令", "", ""),
)


def display_width(text: str) -> int:
    """字符串在终端里占的列数（East Asian 宽 / 全角 = 2，其余 = 1）。"""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格到 ``width``（已超宽则原样返回，不截断）。"""
    return text + " " * max(0, width - display_width(text))


def show_card() -> bool:
    """是否打印卡片：**只在终端**（管道 / 重定向 / systemd 走摘要行）。"""
    return sys.stdout.isatty()


def _display_path(path: str, root: Path) -> str:
    """配置路径的显示形式：在根内 → 相对根的短路径（``config/edge.yml``）；否则原样。

    根外的路径通常是 ``run --config <外部文件>`` 指定的，相对化只会得到一串 ``../..``，
    不如原样给出。
    """
    target = Path(path)
    return str(target.relative_to(root)) if target.is_relative_to(root) else str(target)


def startup_card(
    *,
    version: str,
    root: str,
    config_source: str,
    log_level: str,
    file_logging: bool,
    web_url: str,
    adapter_target: str,
) -> str:
    """启动卡片文本（纯函数：调用方自行 ``print``，便于测试与复用）。"""
    lines = [
        f"  motrix-edge {version}",
        "  " + "─" * _RULE_WIDTH,
        "  " + pad("根目录", _LABEL_WIDTH) + root,
        "  " + pad("配置", _LABEL_WIDTH) + _display_path(config_source, Path(root)),
        "  " + pad("日志", _LABEL_WIDTH) + ("logs/" if file_logging else "logs/（文件日志关闭）"),
        "  " + pad("日志级别", _LABEL_WIDTH) + log_level,
        "  " + pad("服务", _LABEL_WIDTH) + web_url,
        "  " + pad("机器人", _LABEL_WIDTH) + f"{adapter_target} · 等待绑定",
        "",
        "  常用命令（help 看全部）",
    ]
    for command, description, second, second_description in _COMMANDS:
        row = "    " + pad(command, _COMMAND_WIDTH) + pad(description, _DESCRIPTION_WIDTH)
        if second:
            row += pad(second, _COMMAND_WIDTH) + second_description
        lines.append(row.rstrip())
    return "\n".join(lines)


def startup_summary(
    *,
    version: str,
    root: str,
    config_source: str,
    log_level: str,
    file_logging: bool,
    web_url: str,
    adapter_target: str,
) -> str:
    """一行摘要（非终端 / 有文件日志时用）：``key=value``，便于 grep 与归档。"""
    return (
        f"motrix-edge {version} | root={root} | config={config_source} | web={web_url} "
        f"| adapter={adapter_target} | log_level={log_level} | file_logging={'on' if file_logging else 'off'}"
    )


__all__ = ["display_width", "pad", "show_card", "startup_card", "startup_summary"]
