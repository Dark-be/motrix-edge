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

"""Remote-inference settings (vendored from ``lerobot.inference.factory``).

Upstream bases this on a draccus ``ChoiceRegistry`` so the same dataclass can be selected from the
CLI (``--inference.type=remote``). Edge builds the config from its own ``policy`` section, so the
registry machinery is dropped and only the validated fields remain. Field names, defaults and
validation are kept verbatim so a deployment stays interchangeable with an upstream client.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from lerobot.policies import ExecutionMode

from .chunk_settings import chunk_settings


@dataclass
class InferenceEngineConfig:
    """Settings shared by every inference backend."""

    log_level: Literal["INFO", "DEBUG"] = "INFO"

    def __post_init__(self) -> None:
        """Validate the console verbosity shared by every backend."""
        if self.log_level not in {"INFO", "DEBUG"}:
            raise ValueError("Inference log_level must be INFO or DEBUG")

    @property
    def type(self) -> str:
        """Backend name; matches the upstream registry key."""
        return "remote"


@dataclass
class RemoteInferenceConfig(InferenceEngineConfig):
    """Exclusive remote deployment with explicit robot semantics.

    Timing defaults are conservative starting values for lab validation; operators must
    measure their deployment's turnaround and choose a usable playback/age budget.
    """

    endpoint: str = "tcp/127.0.0.1:7447"
    deployment: str = ""
    instance: str | None = None
    expected_artifact: str | None = None
    mode: str = "chunk"
    semantics: str = ""
    # Request when remaining playback reaches this threshold (append, aligned and RTC).
    # Effective floor: recent maximum turnaround + one policy action interval.
    # Smaller values allow more aligned follow-through; aligned task changes bypass this gate.
    refill_seconds: float = 0.5
    # Auto selects aligned plain chunks, leaving RTC continuation unchanged.
    chunk_merge: Literal["auto", "append", "aligned"] = "auto"
    blend_steps: int = 0
    blend_weight: float = 0.5
    blend_components: list[str] = field(default_factory=list)
    max_observation_age_s: float = 5.0
    handshake_timeout_s: float = 10.0
    action_timeout_s: float = 5.0
    language_timeout_s: float = 60.0
    startup_timeout_s: float = 10.0
    action_starvation_grace_s: float = 1.0
    encoding: Literal["raw", "jpeg"] = "raw"
    jpeg_quality: int = 90
    zenoh_config_path: str | None = None
    zenoh_mode: Literal["peer", "client"] = "peer"

    def __post_init__(self) -> None:
        """Validate transport and scheduling settings."""
        super().__post_init__()
        if not math.isfinite(self.action_starvation_grace_s) or self.action_starvation_grace_s < 0:
            raise ValueError("Action starvation grace must be finite and nonnegative")
        if self.zenoh_mode not in {"peer", "client"}:
            raise ValueError("zenoh_mode must be peer (direct) or client (router)")
        if not self.deployment or not self.semantics:
            raise ValueError(
                "Remote inference requires a deployment name and explicit robot/action "
                "semantics (policy.deployment / policy.semantics)"
            )
        try:
            ExecutionMode(self.mode)
        except ValueError as exc:
            raise ValueError(f"Unsupported remote execution mode: {self.mode!r}") from exc
        if self.chunk_merge == "auto":
            self.chunk_merge = "aligned" if self.mode == "chunk" else "append"
        chunk_settings(self.chunk_merge, self.blend_steps, self.blend_weight, self.blend_components)
        if self.mode != "chunk" and self.chunk_merge != "append":
            raise ValueError("chunk_merge=aligned is available only with mode=chunk")
        if self.encoding not in {"raw", "jpeg"} or not 1 <= self.jpeg_quality <= 100:
            raise ValueError("Remote encoding must be raw or jpeg, with jpeg_quality in [1, 100]")
        for name in (
            "refill_seconds",
            "max_observation_age_s",
            "handshake_timeout_s",
            "action_timeout_s",
            "language_timeout_s",
            "startup_timeout_s",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Remote {name} must be finite and positive")


__all__ = ["InferenceEngineConfig", "RemoteInferenceConfig"]
