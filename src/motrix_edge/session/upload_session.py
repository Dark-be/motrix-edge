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

"""UploadSession —— 本地采集 episode 扫描、汇总、选择与上传队列状态。"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class UploadError(Exception):
    """上传会话操作失败；携带 HTTP 语义状态码。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class UploadSession:
    """扫描本地目录并按同名 stem 配对 ``.mcap`` / ``.json`` episode。"""

    _SELECTABLE_STATES = {"ready", "pending", "failed"}

    def __init__(self, base_cfg: dict | None = None):
        cfg = (base_cfg or {}).get("upload", {})
        self.default_folder = cfg.get("data_dir")
        self.endpoint = cfg.get("endpoint")
        self._lock = threading.RLock()
        self._folder_path: str | None = None
        self._scanned_at: str | None = None
        self._episodes: dict[str, dict] = {}
        self._selected: set[str] = set()

    def scan(self, folder_path: str | None = None) -> dict:
        """扫描目录，配对 episode 文件并生成元信息与 SHA-256。"""
        raw_path = folder_path or self.default_folder
        if not raw_path:
            raise UploadError("folder_path is required (or configure upload.data_dir)")
        folder = Path(raw_path).expanduser().resolve()
        if not folder.exists():
            raise UploadError(f"upload folder not found: {folder}", status_code=404)
        if not folder.is_dir():
            raise UploadError(f"upload path is not a directory: {folder}")

        files: dict[str, dict[str, Path]] = {}
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in (".mcap", ".json"):
                files.setdefault(path.stem, {})[path.suffix.lower()] = path

        episodes: dict[str, dict] = {}
        for episode_id in sorted(files, key=self._episode_sort_key):
            pair = files[episode_id]
            old_status = self._episodes.get(episode_id, {}).get("status")
            episode = self._build_episode(episode_id, pair.get(".mcap"), pair.get(".json"))
            if episode["status"] == "ready" and old_status in {"pending", "failed", "succeeded"}:
                episode["status"] = old_status
            episodes[episode_id] = episode

        with self._lock:
            self._folder_path = str(folder)
            self._scanned_at = datetime.now(timezone.utc).isoformat()
            self._episodes = episodes
            self._selected.intersection_update(episodes)
            return self.status()

    def status(self) -> dict:
        """返回当前扫描汇总与选择集。"""
        with self._lock:
            episode_list = []
            for episode_id, episode in self._episodes.items():
                item = dict(episode)
                item["selected"] = episode_id in self._selected
                episode_list.append(item)
            return {
                "folder_path": self._folder_path,
                "scanned_at": self._scanned_at,
                "endpoint_configured": bool(self.endpoint),
                "episode_count": len(episode_list),
                "ready_count": sum(item["status"] == "ready" for item in episode_list),
                "invalid_count": sum(item["status"] == "invalid" for item in episode_list),
                "selected_episode_ids": sorted(self._selected, key=self._episode_sort_key),
                "episodes": episode_list,
            }

    def select(self, episode_ids: list[str]) -> dict:
        """按 episode 标识替换选择集。"""
        requested = list(dict.fromkeys(str(item) for item in episode_ids))
        with self._lock:
            missing = [episode_id for episode_id in requested if episode_id not in self._episodes]
            if missing:
                raise UploadError(f"unknown episode_ids: {missing}", status_code=404)
            invalid = [
                episode_id
                for episode_id in requested
                if self._episodes[episode_id]["status"] not in self._SELECTABLE_STATES
            ]
            if invalid:
                raise UploadError(f"episodes are not selectable: {invalid}", status_code=409)
            self._selected = set(requested)
            return self.status()

    def enqueue(self) -> dict:
        """把选择集标记为 pending；未配置上传目标时返回 501。"""
        with self._lock:
            if not self._selected:
                raise UploadError("no episodes selected", status_code=409)
            if not self.endpoint:
                raise UploadError("upload endpoint is not configured", status_code=501)
            for episode_id in self._selected:
                self._episodes[episode_id]["status"] = "pending"
            return self.status()

    def retry(self) -> dict:
        """把选择集中失败项重置为 pending；实际网络上传留给后续 uploader。"""
        with self._lock:
            failed = [episode_id for episode_id in self._selected if self._episodes[episode_id]["status"] == "failed"]
            if not failed:
                raise UploadError("no failed selected episodes", status_code=409)
            if not self.endpoint:
                raise UploadError("upload endpoint is not configured", status_code=501)
            for episode_id in failed:
                self._episodes[episode_id]["status"] = "pending"
            return self.status()

    @classmethod
    def _build_episode(cls, episode_id: str, mcap_path: Path | None, json_path: Path | None) -> dict:
        errors: list[str] = []
        metadata_content = None
        if mcap_path is None:
            errors.append("missing .mcap file")
        if json_path is None:
            errors.append("missing .json metadata file")
        else:
            try:
                metadata_content = json.loads(json_path.read_text(encoding="utf-8"))
                if not isinstance(metadata_content, dict):
                    errors.append("metadata JSON root must be an object")
                    metadata_content = None
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(f"invalid metadata JSON: {exc}")

        return {
            "episode_id": episode_id,
            "status": "invalid" if errors else "ready",
            "mcap": cls._file_info(mcap_path),
            "metadata": cls._file_info(json_path),
            "metadata_content": metadata_content,
            "errors": errors,
        }

    @staticmethod
    def _file_info(path: Path | None) -> dict | None:
        if path is None:
            return None
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "path": str(path),
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "sha256": digest.hexdigest(),
        }

    @staticmethod
    def _episode_sort_key(episode_id: str):
        prefix, sep, suffix = episode_id.rpartition("_")
        return (prefix if sep else episode_id, int(suffix) if sep and suffix.isdigit() else float("inf"), episode_id)
