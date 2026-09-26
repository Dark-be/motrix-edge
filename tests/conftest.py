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

"""pytest 全局配置：可选面测试的收集开关。

策略客户端 / 服务按**引入的第三方依赖**分组为 pyproject 的可选面（``--extra``）。这些
测试文件在 **import 阶段**就会触碰可选依赖（``websockets`` / ``aiortc`` / ``torch`` +
vendored ``lerobot``），若不预判，缺依赖时会在 collection 阶段直接
``ModuleNotFoundError`` 报错（而不是 skip）。

故在此按依赖是否可导入决定**是否收集**整个文件；未收集的文件名会在 pytest 头上打印，
避免「静默少跑」被误读成全绿。不引入额外依赖的客户端 / 服务（如 RPent 面）无需登记。
"""

import importlib.util

import pytest

# 可选面 → 标志性第三方依赖（与 pyproject 的 extras 对应）
_OPTIONAL_DEPS: dict[str, tuple[str, ...]] = {
    "openpi": ("websockets", "msgpack"),
    "lerobot": ("torch", "grpc"),
    "webrtc": ("aiortc",),
}

# 文件级：**import 阶段**就触碰可选依赖的测试文件 → 缺依赖时整个文件不收集
_OPTIONAL_FILES: dict[str, str] = {
    "test_ws_transport.py": "openpi",
    "test_transport_target.py": "openpi",
    "test_infer_point.py": "openpi",
    "test_webrtc.py": "webrtc",
    "test_lerobot_act_client.py": "lerobot",
}


def _missing(extra: str) -> list[str]:
    """该可选面缺哪些依赖（无缺失 → 空列表）。"""
    return [name for name in _OPTIONAL_DEPS[extra] if importlib.util.find_spec(name) is None]


_MISSING_FILES = {name: extra for name, extra in _OPTIONAL_FILES.items() if _missing(extra)}

collect_ignore = list(_MISSING_FILES)


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "optional(extra): 依赖 pyproject 可选面 extra（缺依赖时跳过）")


def pytest_collection_modifyitems(config, items) -> None:
    """函数级：``@pytest.mark.optional("openpi")`` 的用例在缺依赖时跳过。

    与文件级开关互补——同一文件里只有部分用例需要可选面时（如
    ``tests/test_policy.py`` 的 openpi 用例），用 marker 精确标注，不整文件跳过。
    """
    for item in items:
        marker = item.get_closest_marker("optional")
        if marker is None:
            continue
        extra = marker.args[0] if marker.args else ""
        if extra not in _OPTIONAL_DEPS:
            continue
        missing = _missing(extra)
        if missing:
            reason = f"需要可选面 {extra}：缺 {' / '.join(missing)}（uv sync --extra {extra}）"
            item.add_marker(pytest.mark.skip(reason=reason))


def _skipped_note() -> str | None:
    """「哪些可选面测试没跑」的提示文本（无缺失 → None）。"""
    if not _MISSING_FILES:
        return None
    detail = "、".join(
        f"{name}（缺 {_OPTIONAL_DEPS[extra][0]} → uv sync --extra {extra}）" for name, extra in _MISSING_FILES.items()
    )
    return f"未收集 {len(_MISSING_FILES)} 个可选面测试文件：{detail}"


def pytest_report_header(config) -> str | None:
    """头部提示（``-q`` 下 pytest 不打印 header，故另有 summary 兜底）。"""
    note = _skipped_note()
    return f"optional-deps: {note}" if note else None


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """末尾摘要再提示一次——``-q``（CI 常用）下 header 不显示，避免静默少跑。"""
    note = _skipped_note()
    if note:
        terminalreporter.write_sep("=", f"optional-deps: {note}")
