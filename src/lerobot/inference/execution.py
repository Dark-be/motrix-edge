# Copyright 2026 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Immutable request/result bindings extracted from ``lerobot.inference.execution``.

The scheduling runtime (queues, interpolation commits, freshness accounting) is *not* vendored:
edge's ``motrix_edge.rtc`` owns that. This module keeps only the value object the remote client
serializes into one observation exchange.
"""

from __future__ import annotations

from dataclasses import dataclass

from lerobot.policies import ExecutionMode, QueueSnapshot

from .contracts import ObservationSnapshot


@dataclass(frozen=True)
class ChunkRequest:
    """Immutable observation/continuation binding for one executor operation."""

    observation: ObservationSnapshot
    continuation: QueueSnapshot
    request_id: str
    generation: int
    mode: ExecutionMode
    delay: int
    submitted_at: float
    playback_at_submission: float = 0.0


__all__ = ["ChunkRequest"]
