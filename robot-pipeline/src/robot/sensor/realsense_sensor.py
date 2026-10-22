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

import math
import time

import cv2
import numpy as np
import pyrealsense2 as rs
from utils.data_handler import debug_print
from utils.observation_time import KEY_TIMESTAMP_METADATA, KEY_TIMESTAMP_NS, KEY_TIMESTAMPS_NS

from .sensor import Sensor


def find_device_by_serial(devices, serial):
    for i, dev in enumerate(devices):
        if dev.get_info(rs.camera_info.serial_number) == serial:
            return i
    return None


class RealsenseSensor(Sensor):
    # 支持的 color 输出格式（connect() 用 pixel_format 选择）
    PIXEL_FORMAT_JPG = "jpg"  # color 返回 JPEG 编码 bytes（1 维 ndarray，调用方 imdecode）
    PIXEL_FORMAT_RAW = "raw"  # color 返回原始 RGB ndarray（HxWx3）

    COLOR_SIZE = (640, 480)  # 彩色流分辨率 (width, height)
    DEPTH_SIZE = (640, 480)  # 深度流分辨率 (width, height)；对齐到彩色图后与彩色同网格
    COLOR_FPS = 30
    DEPTH_FPS = 30

    def __init__(self, name):
        super().__init__(name)
        self.enable_depth = False
        self.align_to_color = True  # 深度对齐到彩色图（反投影只需一套彩色内参）
        self.pixel_format = self.PIXEL_FORMAT_RAW  # 默认 raw（硬件 color 流恒为 BGR8）
        self.context = None
        self.devices = None
        self.pipeline = None
        self.config = None
        self.align = None  # rs.align(rs.stream.color)：enable_depth 且对齐时启用
        self.depth_scale = None  # 深度原始值 → 米（RealSense 硬件标定值，典型 0.001）
        self.color_intrinsics = None  # 彩色内参 dict（fx / fy / cx / cy；对齐后深度与彩图共用）

    def connect(self, device, pixel_format="raw", enable_depth=False, align_to_color=True):
        """连接 RealSense，选择 color 输出格式（jpg | raw）与是否采集深度。

        - ``"jpg"`` / ``"jpeg"`` / ``"mjpeg"`` → ``color`` 返回 JPEG 编码 bytes（1 维 ndarray）；
        - ``"raw"`` → ``color`` 返回原始 RGB ndarray（HxWx3）。
        硬件 color 流恒为 BGR8（rs.pipeline），``pixel_format`` 只决定输出编码。

        ``enable_depth=True`` 时额外开 z16 深度流，并在取帧时**把深度对齐到彩色图**
        （``align_to_color``，缺省开）——对齐后 ``depth[v, u]`` 与彩色图 ``color[v, u]`` 是
        同一个物理点，故反投影只需要一套**彩色内参**（不这么做就要处理两套内参间的外参）。
        同时读取硬件 ``depth_scale``（深度原始值 → 米）与彩色内参，见 ``camera_info()``。
        """
        pixel_format = str(pixel_format).lower()
        if pixel_format in ("jpg", "jpeg", "mjpeg"):
            self.pixel_format = self.PIXEL_FORMAT_JPG
        elif pixel_format in ("raw", "rgb"):
            self.pixel_format = self.PIXEL_FORMAT_RAW
        else:
            raise ValueError(f"Unsupported pixel_format: {pixel_format!r} (expected 'jpg' or 'raw')")
        self.enable_depth = bool(enable_depth)
        self.align_to_color = bool(align_to_color)

        self.context = rs.context()
        self.devices = list(self.context.query_devices())

        if not self.devices:
            raise RuntimeError("No RealSense devices found")

        serial = device
        device_idx = find_device_by_serial(self.devices, serial)
        if device_idx is None:
            raise RuntimeError(f"Could not find camera with serial number {serial}")

        # Use SDK clock conversion so camera and joint samples share the Unix clock.
        for sensor in self.devices[device_idx].query_sensors():
            if sensor.supports(rs.option.global_time_enabled):
                sensor.set_option(rs.option.global_time_enabled, 1)
        self.pipeline = rs.pipeline()
        self.config = rs.config()

        self.config.enable_device(serial)
        # self.config.disable_all_streams()
        # Enable color stream only
        self.config.enable_stream(rs.stream.color, *self.COLOR_SIZE, rs.format.bgr8, self.COLOR_FPS)
        if self.enable_depth:
            self.config.enable_stream(rs.stream.depth, *self.DEPTH_SIZE, rs.format.z16, self.DEPTH_FPS)

        try:
            self.pipeline.start(self.config)
            debug_print(self.name, f"Started camera: {self.name} (SN: {serial})", "INFO")
        except RuntimeError as e:
            raise RuntimeError(f"Error starting camera: {str(e)}")
        if self.enable_depth:
            self._load_depth_metadata()
            self.align = rs.align(rs.stream.color) if self.align_to_color else None
            debug_print(
                self.name,
                f"depth enabled (scale={self.depth_scale} m/unit, aligned_to_color={self.align is not None})",
                "INFO",
            )

    def _load_depth_metadata(self) -> None:
        """读硬件深度比例与彩色内参（静态：启动时取一次，见 ``camera_info()``）。"""
        profile = self.pipeline.get_active_profile()
        depth_sensor = profile.get_device().first_depth_sensor()
        self.depth_scale = float(depth_sensor.get_depth_scale())
        color_profile = profile.get_stream(rs.stream.color).as_video_stream_profile()
        intrinsics = color_profile.get_intrinsics()
        self.color_intrinsics = {
            "fx": float(intrinsics.fx),
            "fy": float(intrinsics.fy),
            "cx": float(intrinsics.ppx),
            "cy": float(intrinsics.ppy),
        }

    def camera_info(self) -> dict:
        """相机静态元数据（键名与 edge adapter 的相机元数据契约一致，见 ``http_contract``）。

        ``intrinsics`` = **彩色内参**（深度开启且对齐后，深度图与彩色图共用同一像素网格，
        故它就是反投影用的那套内参）；``depth`` 段（无深度时为 None）含
        ``scale``（原始值 → 米）与 ``aligned_to_color``。
        """
        width, height = self.COLOR_SIZE
        info: dict = {
            "name": self.name,
            "width": int(width),
            "height": int(height),
            "intrinsics": dict(self.color_intrinsics or {}),
            "depth": None,
        }
        if self.enable_depth and self.depth_scale is not None:
            info["depth"] = {
                "scale": self.depth_scale,
                "aligned_to_color": self.align is not None,
            }
        return info

    @staticmethod
    def _frame_time(frame, fallback_ns):
        """Return Unix ns and provenance; never treat a device-relative clock as Unix."""
        metadata = {"source": "host_capture_start"}
        try:
            domain = frame.get_frame_timestamp_domain()
            stamp_ms = float(frame.get_timestamp())
            metadata.update(timestamp_domain=str(domain), frame_timestamp_ms=stamp_ms)
            for name in ("frame_timestamp", "sensor_timestamp", "actual_exposure"):
                field = getattr(rs.frame_metadata_value, name)
                try:
                    if frame.supports_frame_metadata(field):
                        metadata[name + "_us"] = int(frame.get_frame_metadata(field))
                except RuntimeError:
                    continue
            if domain in (rs.timestamp_domain.global_time, rs.timestamp_domain.system_time):
                if math.isfinite(stamp_ms) and stamp_ms > 0:
                    metadata["source"] = str(domain)
                    return round(stamp_ms * 1_000_000), metadata
        except RuntimeError:
            # Metadata/time availability depends on device, firmware and host driver.
            pass
        return fallback_ns, metadata

    def get_information(self):
        """读取完整观测（color 恒取；depth 仅 enable_depth=True 时取），不做 collect_info 过滤。

        深度开启且对齐时先 ``rs.align(color)`` 再取两帧：``depth[v, u]`` 与 ``color[v, u]``
        对应同一物理点（同一帧组，同拍）。
        """
        image = {KEY_TIMESTAMP_NS: time.time_ns()}
        frame = self.pipeline.wait_for_frames()
        # Read original frames before alignment, which changes image geometry.
        times, metadata = {}, {}
        for stream in ("color", "depth") if self.enable_depth else ("color",):
            source_frame = getattr(frame, f"get_{stream}_frame")()
            if not source_frame:
                raise RuntimeError(f"Failed to get {stream} frame.")
            times[stream], metadata[stream] = self._frame_time(source_frame, image[KEY_TIMESTAMP_NS])
        image[KEY_TIMESTAMP_NS] = times["color"]
        image[KEY_TIMESTAMPS_NS] = times
        image[KEY_TIMESTAMP_METADATA] = metadata
        if self.align is not None:
            frame = self.align.process(frame)

        color_frame = frame.get_color_frame()
        if not color_frame:
            raise RuntimeError("Failed to get color frame.")
        tmp_img = np.asanyarray(color_frame.get_data())
        if self.pixel_format == self.PIXEL_FORMAT_JPG:
            # 不需要转换为 BGR 格式，因为 RealSense 输出的已经是 BGR
            image["color"] = cv2.imencode(".jpg", tmp_img)[1]
        else:
            # BGR → RGB；ascontiguousarray 同时保证内存归本拍所有（tmp_img 是 SDK 帧缓冲视图，
            # 下一次同一 pipeline 取帧就会复用），消费方拿到的是可安全跨函数传递的连续数组。
            image["color"] = np.ascontiguousarray(tmp_img[:, :, ::-1])

        if self.enable_depth:
            depth_frame = frame.get_depth_frame()
            if not depth_frame:
                raise RuntimeError("Failed to get depth frame.")
            image["depth"] = np.asanyarray(depth_frame.get_data()).copy()

        return image

    def disconnect(self):
        try:
            self.pipeline.stop()
        except Exception as e:
            debug_print(self.name, f"Pipeline stop failed: {e}", "ERROR")


if __name__ == "__main__":
    cam = RealsenseSensor("test")
    cam.connect("<RealSense 序列号>")  # 现场填写
    cam_list = []
    for i in range(1000):
        print(i)
        data = cam.get_information()["color"]
        cam_list.append(data)
        time.sleep(0.1)
