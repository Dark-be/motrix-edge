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

"""采样时间元数据：Unix 整数纳秒，随数据传递至采集器。"""

KEY_TIMESTAMP_NS = "timestamp_ns"  # sensor 彩色帧的 Unix 时间
KEY_TIMESTAMPS_NS = "timestamps_ns"  # 标准观测：完整话题名 → 采样时间
KEY_TIMESTAMP_METADATA = "timestamp_metadata"  # 逐流 / 逐话题的时间来源信息
