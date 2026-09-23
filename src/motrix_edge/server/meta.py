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

"""server/meta —— 采集元信息选项（``config/capture.yml`` 的 ``meta`` 段）。

配置级、**有意不经命令总线**：选项管理是纯本地配置，经总线就得等节点主循环取命令（submit
最长 5s，且节点 ERROR 时会拒绝），直连 store 才能在任何节点状态下立即生效。CLI 侧继续走
``capture meta`` 命令，两者落到**同一个** ``CaptureMetaStore`` 实例（同一把锁），行为一致。

写操作是受控操作（改的是设备上的配置文件）→ 须持租约；读操作免租约。
``LeaseError`` / ``CaptureMetaError`` 都是 ``ServiceError`` 子类（带 edge 错误码），由 app
层的统一处理器转 HTTP，故此处不 try/except。
"""

from motrix_edge.lease import LeaseManager
from motrix_edge.utils.capture_meta import CaptureMetaStore


class CaptureMetaService:
    """采集元信息选项读写（直连 store）+ 写操作的租约校验。"""

    def __init__(self, store: CaptureMetaStore | None = None, leases: LeaseManager | None = None):
        self._store = store if store is not None else CaptureMetaStore()
        self._leases = leases or LeaseManager()

    def list(self) -> dict:
        """元信息选项全量（前端选择列表用；只读、免租约）。"""
        return {"meta": self._store.list_meta()}

    def add(self, key: str, value: str, lease_id: str | None = None) -> dict:
        """新增选项（分类不存在则创建）；重复 → 400。"""
        return self._write(lease_id, lambda: self._store.add(key, value))

    def edit(self, key: str, old: str, new: str, lease_id: str | None = None) -> dict:
        """重命名选项（``old`` → ``new``）；分类 / 选项不存在 → 400。"""
        return self._write(lease_id, lambda: self._store.edit(key, old, new))

    def delete(self, key: str, value: str, lease_id: str | None = None) -> dict:
        """删除某分类下的选项（分类清空则一并删除）；不存在 → 400。"""
        return self._write(lease_id, lambda: self._store.delete(key, value))

    def delete_key(self, key: str, lease_id: str | None = None) -> dict:
        """删除整个分类；分类不存在 → 400。"""
        return self._write(lease_id, lambda: self._store.delete_key(key))

    def _write(self, lease_id: str | None, action) -> dict:
        """写操作：租约校验 → 执行 → 回**最新全量列表**（与 ``list`` 同构，前端写后无需再拉）。"""
        self._leases.require(lease_id)
        action()
        return {"meta": self._store.list_meta()}


__all__ = ["CaptureMetaService"]
