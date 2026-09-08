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

"""CaptureMetaStore —— 采集元信息选项存储（config/capture.yml）。

元信息选项为**可拓展**的「分类 → 选项数组」结构（如 ``operator``=采集人员、
``task_name``=采集任务），由 ``capture meta`` 命令族（list / add / edit / delete /
delete-key）创建 / 编辑 / 删除；经 ``GET /v1/captures/meta`` 暴露给前端作为选择列表，
选中后由 ``capture sync`` 同步到机器人进程（进程保存一轮数据时附加）。

设计见 wiki/design/motrix_edge_capture_meta.md。
"""

from __future__ import annotations

import threading
from pathlib import Path

import yaml

from motrix_edge.config import writable_config_path
from motrix_edge.utils.load_file import load_yaml


class CaptureMetaError(ValueError):
    """采集元信息操作失败（参数缺失 / 重复 / 不存在等）；携带 HTTP 语义。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class CaptureMetaStore:
    """读写 ``capture.yml`` 的 ``meta`` 段（分类 → 选项数组）。

    线程安全（RLock）：CLI / HTTP 命令可能并发管理选项。选项按添加顺序保持；
    写回时保留文件其它顶层键。``path`` 缺省用**可写配置路径**（外部配置目录
    ``MOTRIX_CONFIG_DIR`` 优先，否则状态目录；首次缺省访问时把包内默认播种到该位置），
    测试可注入临时路径。``meta`` 缺失 / 非映射 / 选项非列表 → 视为空（非法键忽略）。
    """

    def __init__(self, path: str | Path | None = None):
        # 可写配置路径：外部配置目录（MOTRIX_CONFIG_DIR）优先，否则状态目录（包内默认只读）
        if path is None:
            self.path = writable_config_path("capture.yml")
            self._seed_default_if_missing()
        else:
            self.path = Path(path)
        self._lock = threading.RLock()

    def _seed_default_if_missing(self) -> None:
        """无外界配置目录时，把包内默认 ``capture.yml``（只读）播种到可写位置。

        让默认元信息选项开箱可用，同时保留可写性（包内默认本身只读）。
        """
        if self.path.exists():
            return
        from motrix_edge.config import load_config

        default = load_config("capture.yml")
        if not default:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(default, f, allow_unicode=True, sort_keys=False)

    # -- 只读 ---------------------------------------------------------------
    def list_meta(self, key: str | None = None) -> dict:
        """列出全部「分类 → 选项」或某分类选项；分类不存在返回空列表。"""
        meta = self._load()
        if key is None:
            return meta
        return {str(key): meta.get(str(key), [])}

    # -- 增删改 --------------------------------------------------------------
    def add(self, key: str, value: str) -> dict:
        """新增选项；分类不存在则创建。选项重复 → CaptureMetaError。"""
        key, value = self._validate(key, value)
        with self._lock:
            meta = self._load()
            options = meta.setdefault(key, [])
            if value in options:
                raise CaptureMetaError(f"meta option already exists: {key}={value}")
            options.append(value)
            self._save(meta)
            return meta

    def edit(self, key: str, old: str, new: str) -> dict:
        """编辑选项：把 ``old`` 重命名为 ``new``。分类 / 选项不存在 → CaptureMetaError。"""
        key, old = self._validate(key, old)
        if new is None or not str(new).strip():
            raise CaptureMetaError("capture meta edit requires a non-empty <new> value")
        new = str(new).strip()
        with self._lock:
            meta = self._load()
            options = meta.get(key)
            if not options or old not in options:
                raise CaptureMetaError(f"meta option not found: {key}={old}")
            if new in options:
                raise CaptureMetaError(f"meta option already exists: {key}={new}")
            options[options.index(old)] = new
            self._save(meta)
            return meta

    def delete(self, key: str, value: str) -> dict:
        """删除某分类下选项；分类清空则删除分类。分类 / 选项不存在 → CaptureMetaError。"""
        key, value = self._validate(key, value)
        with self._lock:
            meta = self._load()
            options = meta.get(key)
            if not options or value not in options:
                raise CaptureMetaError(f"meta option not found: {key}={value}")
            options.remove(value)
            if not options:
                del meta[key]
            self._save(meta)
            return meta

    def delete_key(self, key: str) -> dict:
        """删除整个分类；分类不存在 → CaptureMetaError。"""
        if key is None or not str(key).strip():
            raise CaptureMetaError("capture meta requires a non-empty <key>")
        key = str(key).strip()
        with self._lock:
            meta = self._load()
            if key not in meta:
                raise CaptureMetaError(f"meta key not found: {key}")
            del meta[key]
            self._save(meta)
            return meta

    # -- 内部 ---------------------------------------------------------------
    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        data = load_yaml(self.path) or {}
        meta = data.get("meta", {}) if isinstance(data, dict) else {}
        if not isinstance(meta, dict):
            return {}
        return {str(k): list(v or []) for k, v in meta.items() if isinstance(v, (list, tuple))}

    def _save(self, meta: dict) -> None:
        data: dict = {}
        if self.path.exists():
            try:
                loaded = load_yaml(self.path)
            except Exception:  # noqa: BLE001 文件损坏：从空字典重建，保留 meta 键
                loaded = {}
            if isinstance(loaded, dict):
                data = loaded
        data["meta"] = meta
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _validate(key, value) -> tuple[str, str]:
        if key is None or value is None:
            raise CaptureMetaError("capture meta requires <key> and <value>")
        key = str(key).strip()
        value = str(value).strip()
        if not key:
            raise CaptureMetaError("capture meta requires a non-empty <key>")
        if not value:
            raise CaptureMetaError("capture meta requires a non-empty <value>")
        return key, value
