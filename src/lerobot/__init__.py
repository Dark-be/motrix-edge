# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
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

"""
Vendored, trimmed subset of `lerobot` (Apache-2.0, HuggingFace Inc.).

Keeps only what the **zenoh remote-inference wire** needs, so edge can talk to
`lerobot-policy-server` without the full training stack (and on Python 3.10):

-   ``lerobot.transport``: zenoh session/subscriber/queryable wrappers.
-   ``lerobot.policies``: ``ExecutionMode`` / ``FeatureSpec`` / ``ChunkPolicySpec`` /
    ``QueueSnapshot`` declarations.
-   ``lerobot.inference``: transport-independent contracts plus the remote settings.
-   ``lerobot.remote_inference``: protocol envelope, msgpack codec and ``RemoteClient``.

The scheduling engines (``RemoteInferenceEngine``, RTC, sync) are intentionally NOT
vendored — ``motrix_edge.rtc`` owns chunk caching, splitting and prefetch.

The original gRPC-based subset (``lerobot.async_inference`` + ``services_pb2``) was removed when
edge moved to the v1 zenoh protocol.
"""

__version__ = "0.6.2"
"""Version of the upstream lerobot this subset was taken from (protocol v1)."""
