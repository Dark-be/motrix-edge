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

import os
import stat
import tempfile
import threading
from pathlib import Path

import yaml

from motrix_edge.config import writable_config_path
from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.utils.data_handler import debug_print
from motrix_edge.utils.load_file import load_yaml


class CaptureMetaError(ServiceError):
    """采集元信息操作失败（参数缺失 / 重复 / 不存在等，缺省 400）。"""

    default_code = ErrorCode.INVALID_ARGUMENT


class CaptureMetaStore:
    """读写 ``capture.yml`` 的 ``meta`` 段（分类 → 选项数组）。

    线程安全（RLock）：CLI / HTTP 命令可能并发管理选项，**读与写共用同一把锁**（读侧
    不会再读到半截文件）；写盘为**原子替换**（临时文件 + ``os.replace``）。选项按添加
    顺序保持；写回时保留文件其它顶层键。``path`` 缺省用**可写配置路径**（外部配置目录
    ``MOTRIX_CONFIG_DIR`` 优先，否则状态目录；首次缺省访问时把包内默认播种到该位置），
    测试可注入临时路径。``meta`` 缺失 / 非映射 / 选项非列表 → 视为空（非法键忽略）。
    """

    def __init__(self, path: str | Path | None = None):
        # 可写配置路径：外部配置目录（MOTRIX_CONFIG_DIR）优先，否则状态目录（包内默认只读）
        if path is None:
            self.path = writable_config_path("capture.yml")
            self._seed_on_access = True  # 惰性播种（构造不做 IO，见 _seed_default_if_missing）
        else:
            self.path = Path(path)
            self._seed_on_access = False
        self._lock = threading.RLock()

    def _seed_default_if_missing(self) -> None:
        """把包内默认 ``capture.yml``（只读）播种到可写位置——**惰性**（首次读 / 写时）。

        构造期不做 IO：配置目录 / 状态目录不可写时（只读挂载、权限受限）不能把
        ``EdgeNode`` / ``CaptureService`` 的构造弄挂。播种失败只记 WARNING（后续读得到
        空集合，写会以明确错误拒绝），不影响节点启动。
        """
        if not getattr(self, "_seed_on_access", False) or self.path.exists():
            return
        from motrix_edge.config import load_config

        default = load_config("capture.yml")
        if not default:
            return
        try:
            self._write_document(default)
        except OSError as exc:  # 只读 / 无权限：降级为「无默认选项」，不阻断启动
            debug_print("capture_meta", f"seed default capture.yml failed: {exc}", "WARNING")

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
        """读取 ``meta`` 段（与写共用同一把锁：并发下不会读到半截文件）。"""
        with self._lock:
            self._seed_default_if_missing()  # 惰性播种：首次读时把包内默认落到可写位置
            meta = self._document().get("meta", {})
            if not isinstance(meta, dict):
                return {}
            return {str(k): list(v or []) for k, v in meta.items() if isinstance(v, (list, tuple))}

    def _document(self) -> dict:
        """当前文件的顶层文档（调用方须持锁）：缺失 → 空字典；损坏 → 也按空字典重建。"""
        if not self.path.exists():
            return {}
        try:
            loaded = load_yaml(self.path)
        except Exception:  # noqa: BLE001 文件损坏：从空字典重建，不阻断后续写
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _save(self, meta: dict) -> None:
        """写回 ``meta`` 段（保留其它顶层键）；原子替换，失败 → ``CaptureMetaError(500)``。"""
        with self._lock:
            data = self._document()
            data["meta"] = meta
            try:
                self._write_document(data)
            except OSError as exc:  # 只读 / 无权限：转成明确的 500，不让 OSError 冒到主循环
                raise CaptureMetaError(f"capture.yml is not writable: {exc}", code=ErrorCode.INTERNAL) from exc

    def _write_document(self, data: dict) -> None:
        """**原子写**：先写同目录临时文件，再 ``os.replace`` 覆盖。

        直接 ``open(path, "w")`` 会被并发读（HTTP ``GET /v1/captures/meta``）读到半截
        YAML → ``load_yaml`` 报错；``os.replace`` 是同目录内的原子改名，读侧只会看到旧版
        或新版。失败抛 ``OSError``（语义由调用方定：播种降级为 WARNING，命令回 500）。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=f".{self.path.name}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
            self._apply_mode(tmp_name)
            os.replace(tmp_name, self.path)
        except BaseException:  # 写 / 替换失败：清掉临时文件，不留垃圾
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _apply_mode(self, tmp_name: str) -> None:
        """对齐临时文件的权限位：``mkstemp`` 建的是 0600，直接替换会让配置文件变成仅属主可读写。

        - 已有文件 → 沿用其权限（管理员怎么设的，写回后不变）；
        - 首次播种（文件不存在）→ 0644：这是给人看 / 给人改的配置文件，与改动前
          ``open(path, "w")``（受 umask 影响，通常 0644）保持一致。
        设权限失败不影响写入本身（退化为 0600），只记 WARNING。
        """
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode) if self.path.exists() else 0o644
            os.chmod(tmp_name, mode)
        except OSError as exc:  # 只读挂载 / 特殊文件系统：不阻断写
            debug_print("capture_meta", f"chmod {tmp_name} failed: {exc}", "WARNING")

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
