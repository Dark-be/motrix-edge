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

"""CaptureMetaStore / capture meta 命令族 / EdgeNode 分发单元测试。"""

import pytest

from motrix_edge.node import EdgeNode
from motrix_edge.utils.capture_meta import CaptureMetaError, CaptureMetaStore
from motrix_edge.utils.commands import (
    CMD_CAPTURE_META_ADD,
    CMD_CAPTURE_META_DELETE,
    CMD_CAPTURE_META_DELETE_KEY,
    CMD_CAPTURE_META_EDIT,
    CMD_CAPTURE_META_LIST,
    CMD_CAPTURE_SYNC,
    build_command_registry,
    handle_capture_meta,
)

# ---------------------------------------------------------------------------
# CaptureMetaStore：config/capture.yml（meta 段）读写 —— 无硬件可跑
# ---------------------------------------------------------------------------


def test_store_missing_file_is_empty(tmp_path):
    store = CaptureMetaStore(tmp_path / "capture.yml")
    assert store.list_meta() == {}
    assert store.list_meta("operator") == {"operator": []}


def test_store_add_creates_category_and_option(tmp_path):
    store = CaptureMetaStore(tmp_path / "capture.yml")
    assert store.add("operator", "张三") == {"operator": ["张三"]}
    assert store.add("operator", "李四") == {"operator": ["张三", "李四"]}
    assert store.add("task_name", "桌面前移") == {"operator": ["张三", "李四"], "task_name": ["桌面前移"]}
    # 文件已落盘
    assert store.list_meta() == {"operator": ["张三", "李四"], "task_name": ["桌面前移"]}
    # 重复 → CaptureMetaError
    with pytest.raises(CaptureMetaError):
        store.add("operator", "张三")


def test_store_edit_renames_option(tmp_path):
    store = CaptureMetaStore(tmp_path / "capture.yml")
    store.add("operator", "张三")
    assert store.edit("operator", "张三", "张三（二期）") == {"operator": ["张三（二期）"]}
    # 分类 / old 不存在 → CaptureMetaError
    with pytest.raises(CaptureMetaError):
        store.edit("operator", "不存在", "王五")
    with pytest.raises(CaptureMetaError):
        store.edit("不存在分类", "张三", "王五")
    # 重命名为已存在 → CaptureMetaError
    store.add("operator", "李四")
    with pytest.raises(CaptureMetaError):
        store.edit("operator", "张三（二期）", "李四")


def test_store_delete_option_and_key(tmp_path):
    store = CaptureMetaStore(tmp_path / "capture.yml")
    store.add("operator", "张三")
    store.add("operator", "李四")
    store.add("task_name", "桌面前移")
    # 删除选项：分类保留
    assert store.delete("operator", "张三") == {"operator": ["李四"], "task_name": ["桌面前移"]}
    # 分类清空 → 自动删除分类
    assert store.delete("operator", "李四") == {"task_name": ["桌面前移"]}
    # 删除整个分类
    assert store.delete_key("task_name") == {}
    # 选项 / 分类不存在 → CaptureMetaError
    with pytest.raises(CaptureMetaError):
        store.delete("operator", "张三")
    with pytest.raises(CaptureMetaError):
        store.delete_key("operator")


def test_store_save_preserves_other_top_level_keys(tmp_path):
    path = tmp_path / "capture.yml"
    path.write_text("meta:\n  operator: [张三]\nother:\n  k: v\n", encoding="utf-8")
    store = CaptureMetaStore(path)
    store.add("task_name", "桌面前移")
    text = path.read_text(encoding="utf-8")
    assert "other:" in text  # 保留 meta 之外的顶层键
    assert store.list_meta() == {"operator": ["张三"], "task_name": ["桌面前移"]}


def test_store_invalid_meta_is_ignored(tmp_path):
    path = tmp_path / "capture.yml"
    path.write_text("meta: not-a-mapping\n", encoding="utf-8")
    store = CaptureMetaStore(path)
    assert store.list_meta() == {}
    store.add("operator", "张三")
    assert store.list_meta() == {"operator": ["张三"]}


def test_store_requires_non_empty_key_value(tmp_path):
    store = CaptureMetaStore(tmp_path / "capture.yml")
    with pytest.raises(CaptureMetaError):
        store.add("", "张三")
    with pytest.raises(CaptureMetaError):
        store.add("operator", "  ")


# ---------------------------------------------------------------------------
# capture meta 命令族：注册表解析 + handle_capture_meta 处理器
# ---------------------------------------------------------------------------


def _registry():
    return build_command_registry()


def test_registry_parses_capture_meta_commands():
    registry = _registry()
    cmd = registry.parse_argv(["capture", "meta", "add", "operator", "张三"])
    assert cmd.name == CMD_CAPTURE_META_ADD
    assert cmd.params == {"key": "operator", "value": "张三"}
    # 最长前缀匹配：capture sync 与 capture meta 互不干扰
    assert registry.parse_argv(["capture", "sync", "--meta", "{}"]).name == CMD_CAPTURE_SYNC
    assert registry.parse_argv(["capture", "meta", "list"]).name == CMD_CAPTURE_META_LIST
    cmd = registry.parse_argv(["capture", "meta", "edit", "operator", "张三", "张三（二期）"])
    assert cmd.name == CMD_CAPTURE_META_EDIT
    assert cmd.params == {"key": "operator", "old": "张三", "new": "张三（二期）"}
    assert registry.parse_argv(["capture", "meta", "delete", "operator", "张三"]).name == CMD_CAPTURE_META_DELETE
    assert registry.parse_argv(["capture", "meta", "delete-key", "operator"]).name == CMD_CAPTURE_META_DELETE_KEY


def test_handle_capture_meta_lifecycle(tmp_path):
    store = CaptureMetaStore(tmp_path / "capture.yml")
    registry = _registry()

    def run_argv(argv):
        return handle_capture_meta(registry.parse_argv(argv), store)

    # list 空
    result = run_argv(["capture", "meta", "list"])
    assert result.status == "ok"
    assert result.data["meta"] == {}
    # add
    result = run_argv(["capture", "meta", "add", "operator", "张三"])
    assert result.status == "ok"
    assert result.data["meta"] == {"operator": ["张三"]}
    # edit
    result = run_argv(["capture", "meta", "edit", "operator", "张三", "张三（二期）"])
    assert result.status == "ok"
    assert result.data["meta"] == {"operator": ["张三（二期）"]}
    # list 单分类
    result = run_argv(["capture", "meta", "list", "operator"])
    assert result.data["meta"] == {"operator": ["张三（二期）"]}
    # delete
    result = run_argv(["capture", "meta", "delete", "operator", "张三（二期）"])
    assert result.status == "ok"
    assert result.data["meta"] == {}
    # delete-key（分类不存在 → rejected）
    result = run_argv(["capture", "meta", "delete-key", "operator"])
    assert result.status == "rejected"
    assert result.status_code == 400


def test_handle_capture_meta_invalid_params(tmp_path):
    store = CaptureMetaStore(tmp_path / "capture.yml")
    registry = _registry()
    result = handle_capture_meta(registry.parse_argv(["capture", "meta", "add"]), store)
    assert result.status == "rejected"
    assert result.status_code == 400
    result = handle_capture_meta(registry.parse_argv(["capture", "meta", "edit", "operator", "a"]), store)
    assert result.status == "rejected"
    assert result.status_code == 400


# ---------------------------------------------------------------------------
# EdgeNode 分发：capture meta 配置级命令「任何状态可用」
# ---------------------------------------------------------------------------


def test_node_dispatches_capture_meta_any_state(tmp_path):
    store = CaptureMetaStore(tmp_path / "capture.yml")
    node = EdgeNode({"identity": {}}, capture_meta_store=store)
    registry = build_command_registry()
    replies = []
    cmd = registry.parse_argv(["capture", "meta", "add", "operator", "王五"])
    cmd.reply_to = replies.append
    node._dispatch(cmd)  # INIT 状态也应响应（配置级命令与状态机解耦）
    assert replies[0].status == "ok"
    assert store.list_meta() == {"operator": ["王五"]}
    # 节点持有同一 store，另一命令读取
    replies2 = []
    cmd2 = registry.parse_argv(["capture", "meta", "list"])
    cmd2.reply_to = replies2.append
    node._dispatch(cmd2)
    assert replies2[0].data["meta"] == {"operator": ["王五"]}
