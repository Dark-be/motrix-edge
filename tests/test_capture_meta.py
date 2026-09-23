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

import stat

import pytest

from motrix_edge.command import (
    CMD_CAPTURE_META_ADD,
    CMD_CAPTURE_META_DELETE,
    CMD_CAPTURE_META_DELETE_KEY,
    CMD_CAPTURE_META_EDIT,
    CMD_CAPTURE_META_LIST,
    CMD_CAPTURE_SYNC,
    build_command_registry,
    handle_capture_meta,
)
from motrix_edge.errors import ErrorCode
from motrix_edge.node import EdgeNode
from motrix_edge.utils.capture_meta import CaptureMetaError, CaptureMetaStore

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


def test_store_write_failure_keeps_previous_content_and_leaves_no_temp(tmp_path, monkeypatch):
    """原子写：写盘失败时原有文件完好（不被截断），且不留下临时文件。

    直接 `open(path, "w")` 会先把文件截空——写盘中途失败（磁盘满 / 断电）就把选项
    列表整份弄丢了；临时文件 + `os.replace` 则是「要么旧版、要么新版」。
    """
    path = tmp_path / "capture.yml"
    path.write_text("meta:\n  operator:\n    - 张三\n", encoding="utf-8")
    store = CaptureMetaStore(path)

    import motrix_edge.utils.capture_meta as module

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(module.yaml, "safe_dump", boom)
    with pytest.raises(CaptureMetaError) as excinfo:
        store.add("operator", "李四")

    assert excinfo.value.code == ErrorCode.INTERNAL  # 写盘失败 → 明确的 500
    assert store.list_meta() == {"operator": ["张三"]}  # 旧内容未被破坏
    assert [item.name for item in tmp_path.iterdir()] == ["capture.yml"]  # 无 .tmp 残留


def test_store_write_keeps_file_mode(tmp_path):
    """原子写不改变已有文件的权限：临时文件是 0600，写回要沿用原权限（如 0640）。"""
    path = tmp_path / "capture.yml"
    path.write_text("meta:\n  operator: [张三]\n", encoding="utf-8")
    path.chmod(0o640)
    store = CaptureMetaStore(path)

    store.add("operator", "李四")

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert store.list_meta() == {"operator": ["张三", "李四"]}


def test_store_seed_creates_readable_file_mode(tmp_path, monkeypatch):
    """首次播种落 0644：配置文件是给人看 / 给人改的，不能因为 mkstemp 变成 0600。"""
    monkeypatch.delenv("MOTRIX_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = CaptureMetaStore()  # 缺省路径 → 状态目录（XDG_STATE_HOME/motrix）

    meta = store.list_meta()  # 首次读触发惰性播种

    assert meta == {"operator": ["张三", "李四"], "task_name": ["桌面前移", "双臂搬运"]}  # 包内默认
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o644


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
    assert result.code == ErrorCode.INVALID_ARGUMENT


def test_handle_capture_meta_invalid_params(tmp_path):
    store = CaptureMetaStore(tmp_path / "capture.yml")
    registry = _registry()
    result = handle_capture_meta(registry.parse_argv(["capture", "meta", "add"]), store)
    assert result.status == "rejected"
    assert result.code == ErrorCode.INVALID_ARGUMENT
    result = handle_capture_meta(registry.parse_argv(["capture", "meta", "edit", "operator", "a"]), store)
    assert result.status == "rejected"
    assert result.code == ErrorCode.INVALID_ARGUMENT


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


def test_store_is_single_instance_across_node_session_and_service(tmp_path):
    """进程内单实例：节点持有的 store 注入给会话与 CaptureService（同一实例 → 同一把锁）。

    防止「各自 new 一个 store、各有一把锁」——那样 CLI（节点态）与 HTTP / 会话态并发写会
    互相覆盖（read-modify-write 丢更新）。
    """
    from fake_robot import FakeRobotAdapter

    from motrix_edge.server.meta import CaptureMetaService
    from motrix_edge.session import get_session

    store = CaptureMetaStore(tmp_path / "capture.yml")
    node = EdgeNode({"identity": {}}, capture_meta_store=store)

    # 会话循环（ACTIVE 态命令）与节点主循环（非任务态命令）共用同一实例
    session = get_session(
        {"identity": {}}, session_type="capture", adapter=FakeRobotAdapter(), capture_meta_store=store
    )
    assert session.capture_meta_store is store

    # server 层（HTTP 端点）由装配层注入节点持有的那一份，而不是各建一个
    assert CaptureMetaService(store=node.capture_meta_store)._store is store
