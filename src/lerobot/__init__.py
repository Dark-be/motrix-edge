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

仅保留与官方 `async_inference` gRPC **wire 互通**所需的最小模块（在仓库内当内置
依赖使用，避免引入完整 `lerobot` + 训练栈依赖）：

-   ``lerobot.transport``：gRPC proto 生成物 + chunk 分片 / pickle 工具（裁剪自
    `src/lerobot/transport`）。
-   ``lerobot.async_inference.helpers``：wire 数据类（TimedData / TimedAction /
    TimedObservation / RemotePolicyConfig），字段布局与官方一致以保证 pickle 互通。

pickle 往返依赖 ``lerobot.*`` 模块路径，故以同名顶层包提供（位于 ``src/lerobot``，
dev/可编辑安装下 ``src`` 在 sys.path，``import lerobot`` 即可用）。
"""
