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
gRPC transport for lerobot async inference（vendored 裁剪版）。

模块不主动导入 grpc（懒加载，避免导入 motrix_edge 时缺 grpc 依赖报错）：
需要时 ``from lerobot.transport.services_pb2_grpc import AsyncInferenceStub``
（该模块顶层才 import grpc）。
"""
