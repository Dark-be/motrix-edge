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

"""Vendored remote-inference contracts (Apache-2.0, HuggingFace Inc.).

Only the transport-independent contracts, one chunk-option validator and the remote settings are
kept; the scheduling engines themselves stay upstream because edge owns its own RTC loop.
"""

from .chunk_settings import chunk_settings
from .contracts import (
    ActionChunk,
    ActionProvenance,
    ObservationSnapshot,
    PolicyCapabilities,
)
from .factory import InferenceEngineConfig, RemoteInferenceConfig
from .execution import ChunkRequest
from lerobot.policies import ExecutionMode, FeatureSpec

__all__ = [
    "ActionChunk",
    "ActionProvenance",
    "ChunkRequest",
    "ExecutionMode",
    "FeatureSpec",
    "InferenceEngineConfig",
    "ObservationSnapshot",
    "PolicyCapabilities",
    "RemoteInferenceConfig",
    "chunk_settings",
]
