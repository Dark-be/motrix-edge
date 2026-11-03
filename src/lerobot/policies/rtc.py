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

"""Continuation snapshot extracted from ``lerobot.policies.rtc.action_queue``.

Only the immutable value object is vendored: edge keeps its own ``rtc`` scheduling, and the remote
client only needs to describe "what the robot is currently committed to" to the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from torch import Tensor


@dataclass
class QueueSnapshot:
    """One coherent continuation. A pop commits an endpoint to interpolation.

    ``cursor`` counts commits since clear, independently of queue replacement;
    ``index`` is retained for the existing local RTC merge API.
    """

    generation: int
    cursor: int
    index: int
    model_actions: Tensor | None
    canonical_actions: Tensor | None
    provenance: tuple[Any, ...]


__all__ = ["QueueSnapshot"]
