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
"""lerobot.utils.constants（vendored 裁剪版）。

裁剪自上游同名文件：仅保留纯字符串常量（无 huggingface_hub / 文件路径依赖）。
"""

OBS_STR = "observation"
OBS_PREFIX = OBS_STR + "."
OBS_ENV_STATE = OBS_STR + ".environment_state"
OBS_STATE = OBS_STR + ".state"
OBS_IMAGE = OBS_STR + ".image"
OBS_IMAGES = OBS_IMAGE + "s"
OBS_LANGUAGE = OBS_STR + ".language"
OBS_LANGUAGE_TOKENS = OBS_LANGUAGE + ".tokens"
OBS_LANGUAGE_ATTENTION_MASK = OBS_LANGUAGE + ".attention_mask"
OBS_LANGUAGE_CAUSAL_MARKS = OBS_LANGUAGE + ".causal_marks"
OBS_LANGUAGE_SUBTASK = OBS_STR + ".subtask"
OBS_LANGUAGE_SUBTASK_TOKENS = OBS_LANGUAGE_SUBTASK + ".tokens"
OBS_LANGUAGE_SUBTASK_ATTENTION_MASK = OBS_LANGUAGE_SUBTASK + ".attention_mask"

ACTION = "action"
ACTION_PREFIX = ACTION + "."
ACTION_TOKENS = ACTION + ".tokens"
ACTION_TOKEN_MASK = ACTION + ".token_mask"
ACTION_CODE_TOKEN_MASK = ACTION + ".code_token_mask"
REWARD = "next.reward"
TRUNCATED = "next.truncated"
DONE = "next.done"
SUCCESS = "next.success"
INFO = "info"
