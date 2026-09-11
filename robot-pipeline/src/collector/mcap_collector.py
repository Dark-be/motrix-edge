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

"""MCAP-format collector（Foxglove，**ROS2 官方消息格式**）。

接收 ``standard_obs`` dict（来自 ``Robot.get_standard_obs()``），把每一条 episode
写成一个 MCAP 文件，文件名用 **UUID**（全局唯一，无编号扫描/冲突）：::

    {uuid4().hex}.mcap

按 ROS2 官方消息格式、每个信号一个 topic（CDR 编码，mcap_ros2）：:

    topic                        schema                               内容
    ---------------------------  -----------------------------------  --------------------------
    observations/qpos            std_msgs/msg/Float64MultiArray       qpos（float64[]）
    observations/images/<cam>    sensor_msgs/msg/CompressedImage      JPEG（format='jpeg'）
    action                       std_msgs/msg/Float64MultiArray       动作（float64[]）

帧时间以 obs 内 ``timestamp``（秒）为准：作消息 ``log_time``/``publish_time``（ns），
并填入 CompressedImage 的 ``header.stamp``。可在 Foxglove 中按时间轴查看。

**流式写入**：``start()`` 打开文件 → 每次 ``collect()`` 直接落盘（不缓冲整段 episode，
内存开销只随单帧大小）→ ``finish()`` 写入 footer 并返回文件路径：:

    c = ActMcapCollector({"save_dir": "./data"})
    c.start()                 # 生成 UUID 文件名打开 .mcap（唯一，无编号冲突）
    c.collect(standard_obs)   # 每帧直接落盘
    c.finish()                # -> 已写入的 .mcap 文件 Path（并写同名 .json 元信息）

结束一轮时写与 mcap 同名的 JSON 元信息文件（``{uuid}.json``），描述该 mcap（见
``_write_meta_json``）。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from utils.base.data_handler import debug_print

# standard_obs keys (see Robot.get_standard_obs())
KEY_QPOS = "observations/qpos"
KEY_ACTION = "action"
CAMERA_PREFIX = "observations/images/"
KEY_TIMESTAMP = "timestamp"  # obs 内帧采集时刻（秒，float；由 Robot.sample_qpos() 提供）

# ROS2 官方消息定义（.msg 展开文本，register_msgdef 使用）
MSG_FLOAT64_MULTI_ARRAY = """\
std_msgs/MultiArrayLayout layout
float64[] data
================================================================================
MSG: std_msgs/MultiArrayLayout
MultiArrayDimension[] dim
uint32 data_offset
================================================================================
MSG: std_msgs/MultiArrayDimension
string label
uint32 size
uint32 stride
"""

MSG_COMPRESSED_IMAGE = """\
std_msgs/Header header
string format
uint8[] data
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
"""

# ROS2 datatype 名
TYPE_FLOAT64_MULTI_ARRAY = "std_msgs/msg/Float64MultiArray"
TYPE_COMPRESSED_IMAGE = "sensor_msgs/msg/CompressedImage"

# topic 名（每信号一个）
TOPIC_QPOS = "observations/qpos"
TOPIC_ACTION = "action"
TOPIC_IMAGE_PREFIX = "observations/images/"


class ActMcapCollector:
    def __init__(self, collector_config: dict):
        self.name = "ActMcapCollector"
        self._save_dir = Path(collector_config.get("save_dir", "./data"))
        self._save_dir.mkdir(parents=True, exist_ok=True)
        # 输入侧兼容 image_format；MCAP 输出恒为 JPEG（CompressedImage format='jpeg'）
        self._image_format = collector_config.get("image_format", "jpeg")
        # 流式状态：start() 打开 writer，collect() 直接落盘，finish() 关闭
        self._writer = None  # 当前 episode 的 mcap writer
        self._fp = None  # 对应文件句柄
        self._schema_qpos = None  # Float64MultiArray schema
        self._schema_img = None  # CompressedImage schema
        self._path: Path | None = None  # 当前 episode 文件路径
        self._step_count = 0  # 当前 episode 已写帧数（sequence）
        # ---- 元信息（finish 时写与 mcap 同名 JSON，描述该 mcap）----
        # 跨 episode 保留的字段（start() 时快照进新 episode）：
        self._identity: dict = {}  # 机器人身份（robot_name / robot_type），set_robot_meta() 设置
        self._pending: dict = {}  # 同步字段（operator / task_name / description ...），set_meta() 设置
        # 当前 episode 的元信息基线（created_at + identity/pending 快照）
        self._meta: dict = {}
        self._start_wall: float | None = None  # 本轮采集开始墙钟时间（duration 兜底）
        self._first_ts: float | None = None  # 首帧 timestamp（秒）
        self._last_ts: float | None = None  # 末帧 timestamp（秒）
        debug_print(
            self.name,
            f"Initialized with save_dir={self._save_dir}, image_format={self._image_format}",
            "INFO",
        )

    # -- public API ---------------------------------------------------------
    def set_save_dir(self, path):
        self._save_dir = Path(path)

    @property
    def save_dir(self) -> Path:
        """当前数据保存目录（供 server 上报 data_status）。"""
        return self._save_dir

    def set_robot_meta(self, robot_name: str = "", robot_type: str = "") -> None:
        """设置机器人身份（env 注入）：JSON 元信息附带 ``robot_name`` / ``robot_type``。

        ``identity`` 跨 episode 保留，每轮 start() 自动快照进本轮元信息基线。
        """
        self._identity["robot_name"] = robot_name or ""
        self._identity["robot_type"] = robot_type or ""

    def _image_to_jpeg(self, val) -> bytes:
        """把单帧图像统一成 JPEG bytes（CompressedImage format='jpeg'）。

        obs 中已是 JPEG bytes（uint8 一维）时直接使用；raw RGB 帧按 ACT/HDF5 约定
        （RGB → BGR）编码为 JPEG。
        """
        import cv2

        is_jpeg = isinstance(val, np.ndarray) and val.ndim == 1 and val.dtype == np.uint8
        if is_jpeg:
            return val.tobytes()
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(np.asarray(val), cv2.COLOR_RGB2BGR))
        if not ok:
            raise ValueError("Failed to encode image as JPEG")
        return buf.tobytes()

    def start(self):
        """开启新一轮 episode 的 mcap 文件（流式：collect 直接落盘）。

        文件名用 UUID（``{uuid4().hex}.mcap``，全局唯一，无编号扫描/冲突）。
        若上一轮未 finish，先防御性关闭（写 footer）再开新文件。
        """
        self._close_writer()
        from mcap_ros2.writer import Writer

        self._path = self._save_dir / f"{uuid.uuid4().hex}.mcap"
        self._fp = open(self._path, "wb")
        self._writer = Writer(self._fp)
        self._schema_qpos = self._writer.register_msgdef(TYPE_FLOAT64_MULTI_ARRAY, MSG_FLOAT64_MULTI_ARRAY)
        self._schema_img = self._writer.register_msgdef(TYPE_COMPRESSED_IMAGE, MSG_COMPRESSED_IMAGE)
        self._step_count = 0
        # 本轮元信息基线（finish 写同名 JSON）：created_at = 采集开始时间（ISO 本地）；
        # identity（robot_name/robot_type）与已同步字段（operator/task_name/...）跨
        # episode 保留，在此快照进本轮基线——采集开始前同步的信息不会因新 episode 丢失。
        self._meta = {"created_at": datetime.now().isoformat(timespec="seconds")}
        self._meta.update(self._identity)
        self._meta.update(self._pending)
        self._start_wall = time.time()
        self._first_ts = None
        self._last_ts = None
        debug_print(
            self.name,
            f"Collector start: {self._path.name}",
            "INFO",
        )

    def collect(self, standard_obs: dict):
        """写入一帧观测（**直接落盘**，不缓冲内存）。需先 ``start()``。

        帧时间以 obs 内 ``timestamp``（秒）为准，转 ns 作 MCAP log_time / header.stamp；
        obs 缺失 timestamp 时回退本地时钟。
        """
        if self._writer is None:
            raise RuntimeError("ActMcapCollector not started; call start() first")

        timestamp_s = standard_obs.get(KEY_TIMESTAMP)
        if timestamp_s is None:
            timestamp_s = time.time()
        # 首末帧时间戳：duration（采集时长）= 末帧 - 首帧
        if self._first_ts is None:
            self._first_ts = timestamp_s
        self._last_ts = timestamp_s
        ts_ns = int(timestamp_s * 1e9)
        sec, nanosec = divmod(ts_ns, 1_000_000_000)
        qpos = np.asarray(standard_obs[KEY_QPOS], dtype=np.float64).tolist()
        action = np.asarray(standard_obs[KEY_ACTION], dtype=np.float64).tolist()
        seq = self._step_count
        self._step_count += 1

        # qpos / action：std_msgs/msg/Float64MultiArray
        for topic, data in ((TOPIC_QPOS, qpos), (TOPIC_ACTION, action)):
            self._writer.write_message(
                topic=topic,
                schema=self._schema_qpos,
                message={"layout": {"dim": [], "data_offset": 0}, "data": data},
                log_time=ts_ns,
                publish_time=ts_ns,
                sequence=seq,
            )
        # images：每相机一个 topic，sensor_msgs/msg/CompressedImage（jpeg）
        for key, val in standard_obs.items():
            if key.startswith(CAMERA_PREFIX) and val is not None:
                cam_name = key[len(CAMERA_PREFIX) :]
                self._writer.write_message(
                    topic=f"{TOPIC_IMAGE_PREFIX}{cam_name}",
                    schema=self._schema_img,
                    message={
                        "header": {
                            "stamp": {"sec": sec, "nanosec": nanosec},
                            "frame_id": "",
                        },
                        "format": "jpeg",
                        "data": self._image_to_jpeg(val),
                    },
                    log_time=ts_ns,
                    publish_time=ts_ns,
                    sequence=seq,
                )

    def finish(self) -> Path | None:
        """结束当前 episode：写入 MCAP footer、写同名 JSON 元信息，返回 mcap 文件路径。

        未 start 过返回 None。JSON 元信息文件与 mcap 同名（``{uuid}.json``，把
        ``.mcap`` 后缀替换为 ``.json``），描述该 mcap（relative_path / robot_name /
        robot_type / operator / task_name / frames / size_bytes / duration / sha256 /
        created_at + 同步字段）。
        """
        path = self._close_writer()
        if path is not None:
            self._write_meta_json(path)
            debug_print(
                self.name,
                f"Collector finish: episode finished, {self._step_count} steps -> {path}",
                "INFO",
            )
        return path

    # ---- 采集元信息（meta）：写入接口统一为 set_*_meta，读取用 meta property ----
    def set_meta(self, meta: dict) -> None:
        """设置同步/附加元信息字段（由 adapter ``capture sync`` 同步：operator / task_name 等）。

        ``pending`` 跨 episode 保留，start() 快照进本轮基线；finish() 写 JSON 时并入。
        ``meta`` 为 None 或空 dict 时安全 no-op。
        """
        self._pending.update(meta or {})
        debug_print(self.name, f"set meta: {meta}", "INFO")

    @property
    def meta(self) -> dict:
        """当前 episode 的元信息（含同步字段；finish 前为部分填充，供 /v1/capture/status 上报）。"""
        meta = {
            "relative_path": self._path.name if self._path else None,
            "robot_name": self._identity.get("robot_name", ""),
            "robot_type": self._identity.get("robot_type", ""),
            "operator": self._pending.get("operator"),
            "task_name": self._pending.get("task_name"),
            "frames": self._step_count,
            "size_bytes": self._path.stat().st_size if self._path and self._path.exists() else None,
            "duration": self._duration_seconds(),
            "sha256": None,  # finish 关闭文件后才计算
            "created_at": self._meta.get("created_at"),
        }
        for key, value in self._pending.items():
            if key not in meta:
                meta[key] = value
        return meta

    def _duration_seconds(self) -> float:
        """本轮采集时长（秒）：首帧 → 末帧 timestamp 之差；无帧时回退墙钟。"""
        if self._first_ts is not None and self._last_ts is not None:
            return max(0.0, self._last_ts - self._first_ts)
        if self._start_wall is not None:
            return max(0.0, time.time() - self._start_wall)
        return 0.0

    def _write_meta_json(self, path: Path) -> Path:
        """写与 mcap 同名的 JSON 元信息文件（``{uuid}.json``），描述该 mcap。

        - 自动统计：``relative_path``（mcap 文件名）/ ``robot_name`` / ``robot_type`` /
          ``frames``（帧数）/ ``size_bytes``（文件字节）/ ``duration``（秒）/ ``sha256``
          （文件哈希）/ ``created_at``（采集开始时间 ISO）；
        - 同步字段：``operator`` / ``task_name`` 未同步以 null 占位（等待 capture sync）；
          其余同步字段（``description`` 等）附加在末尾。
        """
        meta = {
            "relative_path": path.name,
            "robot_name": self._identity.get("robot_name", ""),
            "robot_type": self._identity.get("robot_type", ""),
            "operator": self._pending.get("operator"),
            "task_name": self._pending.get("task_name"),
            "frames": self._step_count,
            "size_bytes": path.stat().st_size,
            "duration": self._duration_seconds(),
            "sha256": self._sha256(path),
            "created_at": self._meta.get("created_at"),
        }
        for key, value in self._pending.items():
            if key not in meta:
                meta[key] = value
        json_path = path.with_suffix(".json")  # episode_N.json（mcap 后缀改为 json）
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        debug_print(self.name, f"Meta written -> {json_path}", "INFO")
        return json_path

    @staticmethod
    def _sha256(path: Path) -> str:
        """mcap 文件 SHA-256 十六进制摘要（流式分块，避免整文件读入内存）。"""
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _close_writer(self) -> Path | None:
        """关闭当前 writer（写 footer）；无打开 writer 时返回 None。"""
        if self._writer is None:
            return None
        try:
            self._writer.finish()
        finally:
            self._writer = None
            if self._fp is not None:
                self._fp.close()
                self._fp = None
        path, self._path = self._path, None
        self._schema_qpos = None
        self._schema_img = None
        return path
