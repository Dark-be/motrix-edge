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

"""UploadSession —— 本地采集 episode 扫描、汇总、选择与打包（上传队列状态）。

**范围**：本版本只做**本地文件查看 / 筛选 / 打包**——数据由数采人员在前端确认后打包，
再**手动上传**到数据平台。程序化上传（消费者为数据平台的上传 API，地址即 ``upload.endpoint``）
留待后续版本；未配置上传目标时 ``enqueue()`` 返回 501，**不删除本地源文件**。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path


class UploadError(Exception):
    """上传会话操作失败；携带 HTTP 语义状态码。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class UploadSession:
    """扫描本地目录并按同名 stem 配对 ``.mcap`` / ``.json`` episode。

    JSON 描述文件按 ``METADATA_SCHEMA`` **schema 驱动**提取为结构化 ``meta`` 字段；
    新增已知字段只需在 schema 加一行，解析与前端展示自动跟随（未知字段保留在
    ``metadata_unknown``，原始 JSON 保留在 ``metadata_content``）。

    上传目标（``upload.endpoint``）是**给数据平台的**：本版本仅维护上传队列状态，
    不做实际网络传输。

    **受控边界**（与 server 层一致）：

      - **目录白名单**：只允许扫描「数据目录」（adapter 上报的采集目录 / ``upload.data_dir``）
        及其子目录——未配置任何允许根 → 409，越界 → 400；
      - **重操作互斥**：``scan`` / ``pack`` 同一时刻只允许一个在跑（都要对整目录算
        SHA-256 / 搬运文件），并发 → 409；
      - **``pack`` 是移动语义**：选中 episode 移入包目录后原位置不再保留（本地整理），
        成功后清空选择集；失败回滚**不删数据**（移回失败的文件保留并汇报路径）。
    """

    _SELECTABLE_STATES = {"ready", "pending", "failed"}

    # ---- 元信息 schema：JSON 描述文件的已知字段 → (类型, 描述) -----------------
    # 类型：str / int / float；类型不符或缺省 → meta 中该字段为 None（不判 invalid）。
    # 新增已知字段只需在此加一行。
    METADATA_SCHEMA: dict[str, tuple[str, str]] = {
        "relative_path": ("str", "相对路径"),
        "robot_name": ("str", "机器人名称"),
        "robot_type": ("str", "机器人类型"),
        "operator": ("str", "采集员"),
        "task_name": ("str", "任务名称"),
        "frames": ("int", "帧数"),
        "size_bytes": ("int", "数据大小（字节）"),
        "duration": ("float", "时长（秒）"),
        "sha256": ("str", "数据 SHA-256"),
        "created_at": ("str", "创建时间"),
    }

    def __init__(self, base_cfg: dict | None = None, allowed_roots=None):
        """
        - ``base_cfg``：读取 ``upload.data_dir`` / ``upload.endpoint``。
        - ``allowed_roots``：**允许扫描的目录白名单**（list[str] 或返回 list 的回调）。
          数据目录在运行时才知道（adapter 上报），故支持回调，并在 server 注入后经
          :meth:`set_allowed_roots` 设置；白名单为空时**拒绝一切扫描**（不默认放开）。
        """
        cfg = (base_cfg or {}).get("upload", {})
        self.default_folder = cfg.get("data_dir")
        self.endpoint = cfg.get("endpoint")
        self._allowed_roots = allowed_roots
        self._lock = threading.RLock()
        self._folder_path: str | None = None
        self._scanned_at: str | None = None
        self._episodes: dict[str, dict] = {}
        self._selected: set[str] = set()
        self._heavy = threading.Lock()  # 重操作互斥（scan / pack 同一时刻只允许一个）

    def set_allowed_roots(self, allowed_roots) -> None:
        """设置允许扫描的目录白名单（list[str] 或返回 list 的回调）。"""
        self._allowed_roots = allowed_roots

    def _roots(self) -> list[Path]:
        """解析白名单（回调 / 列表）+ 配置的缺省目录 → 绝对路径列表。"""
        provider = self._allowed_roots
        # 一律拷贝：provider 可能返回自己的内部 list（下面要 append，不能改到别人的数据）
        raw = list(provider() or []) if callable(provider) else list(provider or [])
        if self.default_folder:
            raw.append(self.default_folder)
        roots: list[Path] = []
        for item in raw:
            if item:
                root = Path(item).expanduser().resolve()
                if root not in roots:
                    roots.append(root)
        return roots

    def _roots_or_raise(self) -> list[Path]:
        """解析允许根；**未配置任何允许根 → 409**（没配置就不默认放开任意路径）。"""
        roots = self._roots()
        if not roots:
            raise UploadError(
                "no allowed upload root configured（请配置 upload.data_dir 或先绑定机器人进程）",
                status_code=409,
            )
        return roots

    def _ensure_allowed(self, folder: Path) -> None:
        """目录白名单校验：只允许扫描「数据目录」及其子目录。

        数据目录 = adapter 上报的采集目录 / ``upload.data_dir``；越过白名单（如 ``/etc``）
        → 400。
        """
        roots = self._roots_or_raise()
        if any(folder == root or folder.is_relative_to(root) for root in roots):
            return
        raise UploadError(
            f"folder_path is outside the allowed upload roots: {folder}（允许：{[str(r) for r in roots]}）",
            status_code=400,
        )

    def _begin_heavy(self, name: str) -> None:
        """占用重操作位（非阻塞）：已有 scan / pack 在跑 → 409。

        scan 要对整个目录算 SHA-256、pack 要复制大文件，两者都不应该并发（否则控制面
        被重复的重 IO 拖住）。
        """
        if not self._heavy.acquire(blocking=False):
            raise UploadError(f"another scan/pack is already in progress（{name} rejected）", status_code=409)

    def _end_heavy(self) -> None:
        """释放重操作位（仅持有者调用；未持有时忽略，重复调用安全）。"""
        try:
            self._heavy.release()
        except RuntimeError:  # 未持有 → 忽略
            pass

    def scan(self, folder_path: str | None = None) -> dict:
        """扫描目录（受控：与 pack 互斥）：配对 episode 文件并生成元信息与 SHA-256。"""
        self._begin_heavy("scan")
        try:
            return self._scan(folder_path)
        finally:
            self._end_heavy()

    def _scan(self, folder_path: str | None = None) -> dict:
        """扫描实现（不自守互斥；pack 成功后重扫复用，此时已持有互斥位）。"""
        roots = self._roots_or_raise()  # 先校验白名单：无允许根 → 409（不默认放开）
        raw_path = folder_path or self.default_folder
        if not raw_path:
            raise UploadError("folder_path is required (or configure upload.data_dir)")
        folder = Path(raw_path).expanduser().resolve()
        if folder not in roots:
            self._ensure_allowed(folder)
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
                "suggested_pack_name": self.suggested_pack_name(),
                "episodes": episode_list,
            }

    def suggested_pack_name(self) -> str:
        """建议包名 ``pack<选中数量>``（未扫描 / 未选择 → 空串）。前端包名输入框预填值。"""
        with self._lock:
            if not self._folder_path or not self._selected:
                return ""
            return f"pack{len(self._selected)}"

    def pack(self, name: str | None = None) -> dict:
        """把**选择集**打包到 ``<扫描目录>/<包名>/``（`.mcap` + `.json`，**移动**）。

        设计见 wiki/design/motrix_edge_upload_session.md：

          - 包名缺省 ``pack<选中数量>``；**目录已存在则拒绝（409）**（不覆盖 / 不合并）——
            重名时由调用方改名重试；非法包名（不是单个安全路径段）→ 400；
          - **移动语义**：源文件进入包目录后从原位置消失（打包是本地整理），成功后清空
            选择集；重扫看不到已打包的 episode（包目录在子目录，扫描不递归）；
          - 源文件缺失 → 404（不建目录）；移动任一文件失败 → **回滚**（已移动的移回原处）
            → 500；**回滚不删数据**：移回失败的文件保留在包目录里，并在错误里给出路径
            （宁可留下残留，也不静默删除——源位置已经不存在了）。
          - **收尾重扫是尽力而为**：文件已全部移动、选择集已清空后，重扫可能因目录被删
            （404）/ 白名单变化（409）失败——此时打包**已经成功**，故只降级为
            ``scan=null`` + ``warnings``，不把成功的打包报成失败。

        受控重操作：与 scan 互斥（并发 → 409）。
        """
        self._begin_heavy("pack")
        try:
            return self._pack(name)
        finally:
            self._end_heavy()

    def _pack(self, name: str | None = None) -> dict:
        """打包实现（不自守互斥；见 :meth:`pack`）。

        **锁粒度**：``self._lock`` 只包住「校验 + 快照搬运计划」与「清空选择集」两小段；
        搬文件（GB 级重 IO）在**锁外**进行——否则前端轮询 ``GET /v1/uploads`` 会被阻塞整个
        打包时长。scan / pack 的并发互斥由 ``_heavy`` 负责，与本锁无关。
        """
        with self._lock:
            if not self._folder_path:
                raise UploadError("scan a folder before packing", status_code=409)
            if not self._selected:
                raise UploadError("no episodes selected", status_code=409)
            folder = Path(self._folder_path)
            # name=None → 缺省 pack<选中数量>；显式传入的空串 / 空白 → 400（不静默用默认名）
            pack_name = self._validate_pack_name(name if name is not None else f"pack{len(self._selected)}")
            target = folder / pack_name
            if target.exists():
                raise UploadError(
                    f"pack folder already exists: {target.name}（请改名后重试）",
                    status_code=409,
                )
            episode_ids = sorted(self._selected, key=self._episode_sort_key)
            sources: list[Path] = []
            for episode_id in episode_ids:
                episode = self._episodes[episode_id]
                sources.extend(
                    Path(info["path"])
                    for info in (episode.get("mcap"), episode.get("metadata"))
                    if info and info.get("path")
                )
            missing = [str(path) for path in sources if not path.is_file()]
            if missing:
                raise UploadError(f"source files missing: {missing}", status_code=404)

        moved: list[Path] = []
        try:
            target.mkdir(parents=False)  # 仅建一级：包目录父目录必须已存在
            for source in sources:
                shutil.move(str(source), str(target / source.name))
                moved.append(source)
        except OSError as exc:
            leftover = self._rollback_moves(target, moved)
            if leftover:
                raise UploadError(
                    f"pack failed: {exc}；以下文件移回失败，已保留在 {target}：{leftover}",
                    status_code=500,
                ) from exc
            raise UploadError(f"pack failed: {exc}", status_code=500) from exc

        with self._lock:
            # 只清「本次打包走的」那些：并发到来的新选择不受影响（重扫也会丢掉已消失的 id）
            self._selected.difference_update(episode_ids)

        # 收尾重扫：**发包此刻已经成功**（文件已移动、选择集已清空），重扫失败不能把调用方
        # 当成失败——扫描目录中途被删（404）、adapter 数据目录掉线使白名单变化（409）都会
        # 命中。降级为「scan=null + warnings」，前端据 warnings 提示要重新扫描。
        warnings: list[str] = []
        scan: dict | None = None
        try:
            scan = self._scan(str(folder))
        except (UploadError, OSError) as exc:
            warnings.append(f"post-pack rescan failed: {exc}")
        return {
            "name": pack_name,
            "path": str(target),
            "episode_count": len(episode_ids),
            "episode_ids": episode_ids,
            "file_count": len(sources),
            "files": [str(target / source.name) for source in sources],
            "scan": scan,  # 重扫失败 → null（打包本身仍成功，见 warnings）
            "warnings": warnings,
        }

    @staticmethod
    def _rollback_moves(target: Path, moved: list[Path]) -> list[str]:
        """回滚：把已移入包目录的文件尽量移回原位，返回**移回失败**的路径列表。

        **绝不删数据**：移回失败的文件保留在包目录里，由调用方在错误信息中汇报路径——
        源位置已经不存在，删掉即永久丢失（宁可留下残留让人来收拾）。
        全部移回成功时顺手删掉刚建的空包目录。
        """
        leftover: list[str] = []
        for source in moved:
            try:
                shutil.move(str(target / source.name), str(source))
            except OSError:  # noqa: PERF203 移回失败：保留残留文件（不删数据）
                leftover.append(str(target / source.name))
        if not leftover:
            try:
                target.rmdir()  # 仅删空目录（此时已无残留文件）
            except OSError:
                pass
        return leftover

    @staticmethod
    def _validate_pack_name(name: str) -> str:
        """校验包名是**单个安全路径段**（不得逃出扫描目录 / 生成隐藏目录）→ 返回规范化名。"""
        candidate = str(name).strip()
        if not candidate:
            raise UploadError("pack name is required")
        if len(candidate) > 64:
            raise UploadError("pack name is too long (max 64 characters)")
        if candidate in (".", "..") or candidate.startswith("."):
            raise UploadError(f"invalid pack name: {candidate!r}（不得以 . 开头）")
        if any(sep in candidate for sep in ("/", "\\")) or any(ch in candidate for ch in "\0\n\r\"'*:<>?|"):
            raise UploadError(f"invalid pack name: {candidate!r}（只能是单个目录名）")
        return candidate

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

        meta, metadata_unknown = cls._extract_metadata(metadata_content)

        # 文件信息（全量 SHA-256）放在最后取：扫描时长与数据量成正比，期间文件可能被手工
        # 清理 / 搬走（如按 pack 失败回执去收拾包目录残留）。单文件失联只降级本 episode，
        # 不让异常冒到 HTTP 层使整次扫描结果作废（与 .json 读取的兜底对称）。
        mcap_info, mcap_error = cls._file_info(mcap_path, ".mcap")
        json_info, json_error = cls._file_info(json_path, ".json")
        errors.extend(error for error in (mcap_error, json_error) if error is not None)

        return {
            "episode_id": episode_id,
            "status": "invalid" if errors else "ready",
            "mcap": mcap_info,
            "metadata": json_info,
            "meta": meta,
            "metadata_content": metadata_content,
            "metadata_unknown": metadata_unknown,
            "errors": errors,
        }

    @classmethod
    def _extract_metadata(cls, content: dict | None) -> tuple[dict, dict]:
        """按 schema 提取结构化字段并归一化类型；未知字段保留在 metadata_unknown。

        - ``meta``：已知字段（类型归一化：int / float / str）；JSON 缺失或类型不符的
          schema 字段补 ``None``（可空，不判 invalid），前端可稳定遍历。
        - ``metadata_unknown``：schema 未识别的原始字段（向前兼容新数据）。
        """
        if not isinstance(content, dict):
            return {}, {}
        meta: dict = {}
        unknown: dict = {}
        for key, value in content.items():
            spec = cls.METADATA_SCHEMA.get(key)
            if spec is None:
                unknown[key] = value
                continue
            kind = spec[0]
            try:
                if kind == "int":
                    meta[key] = int(value)
                elif kind == "float":
                    meta[key] = float(value)
                else:
                    meta[key] = str(value)
            except (TypeError, ValueError):
                meta[key] = None  # 类型不符 → 可空
        for key in cls.METADATA_SCHEMA:  # 补齐缺失字段为 None
            meta.setdefault(key, None)
        return meta, unknown

    @staticmethod
    def _file_info(path: Path | None, label: str) -> tuple[dict | None, str | None]:
        """读文件元信息 + 全量 SHA-256 → ``(info, error)``。

        ``path`` 为 None（配对时就不存在）→ ``(None, None)``，由调用方按 missing 处理；
        **文件在扫描期间消失 / 不可读 → ``(None, 错误说明)``**：调用方把该 episode 标
        invalid 并在 ``errors`` 里注明，而不是让 ``FileNotFoundError`` 冒到 HTTP 层把整次
        扫描结果丢掉（扫描要算全量哈希，这个窗口随数据量线性变大）。
        """
        if path is None:
            return None, None
        try:
            stat = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as src:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(chunk)
        except FileNotFoundError:
            return None, f"{label} file vanished during scan: {path}"
        except OSError as exc:  # 权限 / IO 错误同样只降级单个 episode
            return None, f"{label} file unreadable: {exc}"
        return {
            "path": str(path),
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "sha256": digest.hexdigest(),
        }, None

    @staticmethod
    def _episode_sort_key(episode_id: str):
        prefix, sep, suffix = episode_id.rpartition("_")
        return (prefix if sep else episode_id, int(suffix) if sep and suffix.isdigit() else float("inf"), episode_id)
