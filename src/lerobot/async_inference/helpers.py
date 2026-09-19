# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
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
async_inference wire 数据类（vendored 裁剪版）。

裁剪自 ``src/lerobot/async_inference/helpers.py``：仅保留与官方 ``policy_server``
pickle 往返所需的数据类（TimedData / TimedAction / TimedObservation /
RemotePolicyConfig）。**字段名/继承布局须与官方一致**（pickle 按 ``__dict__``
还原，模块路径 = ``lerobot.async_inference.helpers`` 使官方服务端可正确反序列化）。
"""

from dataclasses import dataclass, field
from typing import Any

# 动作载荷类型：官方为 torch.Tensor（TimedAction.action）。edge 反序列化需 CPU torch。
Action = Any

# 观测为 dict（值可为标量 / numpy / uint8 图像），由服务端 build_dataset_frame 规范化。
RawObservation = dict[str, Any]


@dataclass
class TimedData:
    """带 timestamp / timestep 的数据基类（wire 兼容）。"""

    timestamp: float
    timestep: int

    def get_timestamp(self):
        return self.timestamp

    def get_timestep(self):
        return self.timestep


@dataclass
class TimedAction(TimedData):
    action: Action

    def get_action(self):
        return self.action


@dataclass
class TimedObservation(TimedData):
    observation: RawObservation
    must_go: bool = False

    def get_observation(self):
        return self.observation


@dataclass
class RemotePolicyConfig:
    """SendPolicyInstructions 载荷：下发策略配置（wire 兼容官方 RemotePolicyConfig）。

    字段名须与官方一致（pickle 状态按名还原）：policy_type / pretrained_name_or_path /
    lerobot_features / actions_per_chunk / device / rename_map。
    ``lerobot_features`` 为 dataset-format 特征字典（形如 hw_to_dataset_features 输出：
    ``{"observation.state": {...}, "observation.images.<cam>": {...}}``），edge 按
    启用臂/相机自建。
    """

    policy_type: str
    pretrained_name_or_path: str
    lerobot_features: dict[str, dict]
    actions_per_chunk: int
    device: str = "cpu"
    rename_map: dict[str, str] = field(default_factory=dict)
