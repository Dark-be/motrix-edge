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

"""LLM 轨迹策略客户端：把「图像 + 状态 + 任务指令」交给云端 VLM，取回稀疏笛卡尔轨迹并
重采样成控制步动作块（设计见 wiki/design/motrix_edge_llm_policy.md）。

- **语言条件策略**：``requires_prompt = True``——推理前必须 ``infer prompt <text>`` 预置任务
  指令（如「把红色方块叠到蓝色方块上」），它同时是 rollout 录制时的 ``task_name``。
- **动作空间**：``ActionSpace.CARTESIAN_POSE``（每臂 7 维 = xyz + rpy + 夹爪）——笛卡尔 →
  关节的 IK 由机器人侧承担，edge 只承载位姿语义。
- **密钥**：``api_key`` **运行时值优先**（前端表单 / ``infer config set`` 写入内存态，结构标记
  ``secret``：不落盘、不回显、日志脱敏）；留空时回退**环境变量**（默认 ``OPENAI_API_KEY``，
  变量名可经 ``edge.yml`` 的 ``policy.api_key_env`` 改名——它**不是表单项**，不出现在前端 /
  ``infer config`` 里）；
- **命中端点**：OpenAI 兼容 ``POST {base_url}/chat/completions``（图像 base64 内联）；
- **失败即不下发**：网络 / 超时 / 解析 / 校验失败一律返回 ``None``（RTC 计入 ``failed_chunks``，
  本步跳过、机械臂保持当前目标），不升级为任务错误。

块缓存 / 三元切分 / 过渡 / 异步预取由 ``motrix_edge.rtc`` 负责——本客户端只做「取一次原始块」。
"""

from __future__ import annotations

import base64
import os

import cv2
import httpx
import numpy as np

from motrix_edge.adapter.base import ActionSpace
from motrix_edge.policy.base import BasePolicyClient
from motrix_edge.policy.contract import to_rgb_uint8
from motrix_edge.policy.llm.trajectory import (
    ACTION_DIM_PER_ARM,
    DEFAULT_MAX_POINTS,
    TrajectoryError,
    hold_from_observation,
    trajectory_block,
)
from motrix_edge.rtc import ActionChunk
from motrix_edge.utils.data_handler import debug_print

# 观测键（与 adapter 契约一致；此处显式引用避免 import 环）
KEY_OBS_QPOS = "observations/qpos"
KEY_OBS_POSE = "observations/pose"
KEY_OBS_IMAGE_PREFIX = "observations/images/"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_HORIZON = 15
DEFAULT_IMAGE_SIZE = 768
DEFAULT_HISTORY_LEN = 3
DEFAULT_TIMEOUT = 30.0

# 默认系统提示：坐标系 / 输出契约 / 安全约定的单点定义（可经配置项 system_prompt 覆盖）
DEFAULT_SYSTEM_PROMPT = (
    "You control a robot arm to complete a manipulation task (e.g. stacking blocks).\n"
    "You see camera images and the robot state (end-effector pose and gripper per arm).\n"
    "Reply with a SINGLE JSON object, no markdown, no extra text:\n"
    '{"reasoning": "<one short sentence>", "trajectory": ['
    '{"t": <seconds from now>, "arm": "<arm name>", "pos": [x, y, z], '
    '"rot": [rx, ry, rz], "gripper": <0..1>}, ...]}\n'
    "Rules:\n"
    "- pos is the end-effector position in meters in the robot base frame; rot is orientation in "
    "radians (same convention as the reported pose); gripper 0 = open, 1 = closed.\n"
    "- t is non-decreasing and the first point is t = 0 (the current state).\n"
    "- Give a sparse sequence of waypoints (5-15 points) covering roughly the next few seconds; "
    "interpolation between waypoints is linear, so keep motions slow and monotonic per segment.\n"
    "- Do not jump: consecutive waypoints should differ by at most a few centimetres.\n"
    "- Always declare the arm for every waypoint. Arms not mentioned keep their current position.\n"
    "- Keep the object inside the workspace; if the task is already done, repeat the current pose."
)


class LLMPolicyClient(BasePolicyClient):
    """云端 LLM/VLM 轨迹策略客户端（OpenAI 兼容 chat completions）。"""

    requires_prompt = True  # 语言条件策略：推理前必须 infer prompt 预置任务指令
    action_space = ActionSpace.CARTESIAN_POSE  # 输出笛卡尔位姿轨迹（机器人侧 IK 执行）
    HISTORY_ENTRY_MAX = 200  # 历史条目字符上限（结构化摘要正常远低于此；超限即截断并告警）

    def __init__(self, policy_config: dict):
        super().__init__(policy_config=policy_config)
        cfg = self.policy_config
        self.base_url = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.model = cfg.get("model")
        # 兜底环境变量名：读 yaml 里的 policy.api_key_env（**不是表单项**，默认 OPENAI_API_KEY）
        self.api_key_env = str(cfg.get("api_key_env") or DEFAULT_API_KEY_ENV)
        self.timeout = float(cfg.get("timeout") or DEFAULT_TIMEOUT)
        self.temperature = float(cfg.get("temperature") or 0.0)
        self.max_tokens = int(cfg.get("max_tokens") or 1024)
        self.horizon = int(cfg.get("horizon") or DEFAULT_HORIZON)
        self.image_size = int(cfg.get("image_size") or DEFAULT_IMAGE_SIZE)
        self.history_len = int(cfg.get("history_len") if cfg.get("history_len") is not None else DEFAULT_HISTORY_LEN)
        self.max_points = int(cfg.get("max_points") or DEFAULT_MAX_POINTS)
        self.max_pose_step = float(cfg.get("max_pose_step") or 0.0)
        self.system_prompt = str(cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT)
        self.prompt = cfg.get("prompt")

        # 布局（由会话经 bind_adapter 注入；``policy.arms`` 为未绑定时的显式回退，供离线单测）
        self._arms: list[str] = [str(a) for a in (cfg.get("arms") or [])]
        self._camera_names: list[str] | None = None

        self._http: httpx.Client | None = None
        self._connected = False
        self._history: list[str] = []  # 历史摘要（文本；不回灌历史图像）
        self._pose_missing_logged = False  # 缺位姿观测只提醒一次（避免每块刷日志）

    # ---- 连接 ----------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def arms(self) -> list[str]:
        """生效的臂顺序（动作块分段布局的单一来源：adapter 启用臂 > 配置 ``arms``）。"""
        return list(self._arms)

    @property
    def action_dim(self) -> int:
        """动作维度（每臂 7 维 × 臂数）。"""
        return ACTION_DIM_PER_ARM * len(self._arms)

    def _api_key(self) -> str | None:
        """生效的 API key：**运行时值优先**（前端表单 / ``infer config set`` 写入内存态），

        回退环境变量（变量名 = ``policy.api_key_env``，缺省 ``OPENAI_API_KEY``）。运行时值标记为
        ``secret``：不落盘、不回显、日志脱敏。两边都为空 → ``None``（不建立连接）。
        """
        runtime = self.policy_config.get("api_key")
        if runtime and str(runtime).strip():
            return str(runtime).strip()
        return os.environ.get(self.api_key_env) or None

    def _api_key_source(self) -> str | None:
        """密钥来源（``runtime`` / ``env`` / None）——供状态上报，**不回显密钥本身**。"""
        if self.policy_config.get("api_key") and str(self.policy_config["api_key"]).strip():
            return "runtime"
        return "env" if os.environ.get(self.api_key_env) else None

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout)
        return self._http

    def connect(self):
        """校验配置并标记已连接（不做推理；健康探测在 ``prepare``）。"""
        if not self.model:
            raise ValueError("llm policy requires 'model' (set via infer config set)")
        if not self._api_key():
            raise ValueError(
                f"llm policy requires API key: fill 'api_key' in the panel (edge memory only) "
                f"or set environment variable {self.api_key_env!r}"
            )
        self._connected = True
        self.server_metadata = {"model": self.model, "base_url": self.base_url}

    def prepare(self, observation=None):
        """预热：``GET {base_url}/models`` 校验端点 / 密钥（**不跑推理**，避免额外 token 成本）。

        失败不致命（记录后由首个 rollout 重试），与其它策略的预热语义一致。
        """
        if not self._connected:
            return
        try:
            resp = self._client().get(f"{self.base_url}/models", headers=self._headers())
            debug_print("LLMPolicyClient", f"prepare: /models -> {resp.status_code}", "INFO")
        except Exception as exc:  # noqa: BLE001 预热失败不影响后续推理
            debug_print("LLMPolicyClient", f"prepare failed: {exc}", "WARNING")

    def _headers(self) -> dict:
        key = self._api_key()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def disconnect(self):
        """释放 HTTP 连接池（幂等）。"""
        if self._http is not None:
            self._http.close()
            self._http = None
        self._connected = False

    def reset(self):
        """复位会话状态：清空历史摘要（每次 ``infer rollout`` 重新开始推理）。"""
        self._history.clear()

    # ---- 布局绑定 -------------------------------------------------------------
    def bind_adapter(self, action_dim=None, camera_names=None, arms=None):
        """绑定 adapter 运行时启用布局：臂名（动作块分段顺序）+ 启用相机名。

        ``arms`` 为空时按 ``action_dim`` 推导臂数（每臂 7 维）——仅作为无法获得臂名时的兜底，
        正常路径由会话传入 adapter 的 ``enabled_arms``（臂名的单一事实来源）。
        """
        if arms:
            self._arms = [str(a) for a in arms]
        elif action_dim and not self._arms:
            count = max(1, int(action_dim) // ACTION_DIM_PER_ARM)
            self._arms = [f"arm{i + 1}" for i in range(count)]
        self._camera_names = [str(c) for c in camera_names] if camera_names else None
        debug_print(
            "LLMPolicyClient",
            f"bound layout: arms={self._arms}, cameras={self._camera_names}",
            "INFO",
        )

    # ---- 推理 ----------------------------------------------------------------
    def infer_chunk(self, observation, index: int | None = None) -> ActionChunk | None:
        """请求一次推理并返回**原始动作块**（``[H, dim]`` 笛卡尔轨迹，``dim = 7 × 臂数``）。

        任何失败（未连接 / 无 prompt / 网络 / 解析 / 校验）→ ``None``（本步不下发）。
        ``index`` = 当前绝对步号，仅回填 ``ActionChunk.start_index``（RTC 据此对齐块起始）。
        """
        try:
            return self._infer(observation, index)
        except TrajectoryError as exc:
            debug_print("LLMPolicyClient", f"trajectory rejected: {exc}", "WARNING")
        except Exception as exc:  # noqa: BLE001 网络 / 解析 / 维度异常一律不下发
            debug_print("LLMPolicyClient", f"inference failed: {exc}", "WARNING")
        return None

    def _infer(self, observation, index: int | None) -> ActionChunk | None:
        if not self._connected:
            self._connected = self._api_key() is not None and bool(self.model)
            if not self._connected:
                return None
        if not self._arms:
            debug_print("LLMPolicyClient", "no arm layout bound (bind_adapter / policy.arms)", "WARNING")
            return None
        task = (self.prompt or "").strip()
        if not task:
            debug_print("LLMPolicyClient", "prompt required: set via 'infer prompt <text>'", "WARNING")
            return None

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self._user_content(observation, task)},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        resp = self._client().post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers())
        resp.raise_for_status()
        content = self._extract_content(resp.json())

        hold = hold_from_observation(observation.get(KEY_OBS_POSE), observation.get(KEY_OBS_QPOS), self._arms)
        block = trajectory_block(
            content,
            arms=self._arms,
            horizon=self.horizon,
            hold=hold,
            max_points=self.max_points,
            max_pose_step=self.max_pose_step,
        )
        if block.shape[1] != self.action_dim:
            raise TrajectoryError(f"trajectory dim {block.shape[1]} != expected {self.action_dim}")
        self._remember(self._summarize_block(block))
        debug_print(
            "LLMPolicyClient",
            f"trajectory -> block {block.shape} (horizon={self.horizon}, arms={self._arms})",
            "INFO",
        )
        return ActionChunk(actions=block, start_index=int(index or 0))

    @staticmethod
    def _extract_content(body: dict) -> str:
        """从 chat completions 响应取助手文本（键缺失 → 抛错，由上层转成「不下发」）。

        ``finish_reason=length`` = 响应被 ``max_tokens`` 截断（JSON 多半不完整）→ 告警，
        提示上调 ``max_tokens``。
        """
        choices = (body or {}).get("choices") or []
        if not choices:
            raise TrajectoryError("model response has no choices")
        choice = choices[0] or {}
        if str(choice.get("finish_reason") or "").lower() == "length":
            debug_print(
                "LLMPolicyClient",
                "response truncated by max_tokens (finish_reason=length): raise 'max_tokens' "
                "if the trajectory JSON came out incomplete",
                "WARNING",
            )
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 部分兼容端点返回内容块数组
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not content:
            raise TrajectoryError("model response has empty content")
        return str(content)

    def _grippers(self, qpos) -> dict:
        """每臂夹爪开合（``observations/qpos`` 每臂最后一维；缺观测 → 空 dict）。

        关节角本身**不下发**给模型（笛卡尔策略用不到），只有夹爪这一维从关节布局里取。
        """
        if qpos is None:
            return {}
        values = np.asarray(qpos, dtype=np.float64).reshape(-1)
        grippers = {}
        for index, arm in enumerate(self._arms):
            last = index * ACTION_DIM_PER_ARM + ACTION_DIM_PER_ARM - 1
            if values.shape[0] > last:
                grippers[arm] = float(np.clip(values[last], 0.0, 1.0))
        return grippers

    def _user_content(self, observation, task: str) -> list[dict]:
        """组装用户消息：任务指令 + 末端位姿 / 夹爪状态 + 历史摘要 + 启用相机图像（base64）。

        状态只给**末端位姿 + 夹爪**（每臂一行）：关节角不下发——笛卡尔策略的输入空间是位姿，
        关节值对模型无信息量，白花 token。
        """
        lines = [f"Task: {task}", f"Current state (arms in order {self._arms}):"]
        pose = observation.get(KEY_OBS_POSE)
        grippers = self._grippers(observation.get(KEY_OBS_QPOS))
        if pose is None and not self._pose_missing_logged:
            self._pose_missing_logged = True  # 只提醒一次（每块都提醒会刷日志）
            debug_print(
                "LLMPolicyClient",
                f"no '{KEY_OBS_POSE}' in observation: cartesian state unavailable "
                "(robot must report end-effector pose, see shm pose_dim)",
                "WARNING",
            )
        if pose is not None:
            values = np.asarray(pose, dtype=np.float64).reshape(-1)
            for index, arm in enumerate(self._arms):
                segment = values[index * 6 : index * 6 + 6]
                if segment.size != 6:
                    continue
                pos = ", ".join(f"{v:.3f}" for v in segment[:3])
                rot = ", ".join(f"{v:.3f}" for v in segment[3:])
                line = f"- {arm}: pose_pos=[{pos}] pose_rot=[{rot}]"
                if arm in grippers:
                    line += f" gripper={grippers[arm]:.2f}"
                lines.append(line)
        if self._history:
            lines.append("Recent chunks (oldest first):")
            lines.extend(f"- {entry}" for entry in self._history)

        content: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
        for key, value in observation.items():
            if not key.startswith(KEY_OBS_IMAGE_PREFIX):
                continue
            name = key[len(KEY_OBS_IMAGE_PREFIX) :]
            if self._camera_names is not None and name not in self._camera_names:
                continue
            encoded = self._encode_image(value)
            if encoded is None:
                continue
            content.append({"type": "text", "text": f"Image: {name}"})
            content.append({"type": "image_url", "image_url": {"url": encoded}})
        return content

    def _encode_image(self, value) -> str | None:
        """观测图像（JPEG bytes / ndarray）→ ``data:image/jpeg;base64,...``（最长边 ``image_size``）。"""
        try:
            rgb = to_rgb_uint8(value)
            height, width = rgb.shape[:2]
            longest = max(height, width)
            if self.image_size > 0 and longest > self.image_size:
                scale = self.image_size / float(longest)
                rgb = cv2.resize(
                    rgb,
                    (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
            ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            if not ok:
                return None
            return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
        except Exception as exc:  # noqa: BLE001 单张图像失败只跳过该张
            debug_print("LLMPolicyClient", f"image encode failed: {exc}", "WARNING")
            return None

    def _remember(self, entry: str) -> None:
        """记录本轮动作摘要（结构化文本，控制 token 增长）；超长截断并告警。"""
        summary = " ".join(str(entry or "").split())
        if len(summary) > self.HISTORY_ENTRY_MAX:
            debug_print(
                "LLMPolicyClient",
                f"history entry truncated to {self.HISTORY_ENTRY_MAX} chars (was {len(summary)}): {summary[:80]}...",
                "WARNING",
            )
            summary = summary[: self.HISTORY_ENTRY_MAX]
        self._history.append(summary)
        if self.history_len <= 0:
            self._history.clear()
        elif len(self._history) > self.history_len:
            del self._history[: len(self._history) - self.history_len]

    def _summarize_block(self, block: np.ndarray) -> str:
        """动作块 → **结构化历史摘要**（每臂一段：起点 → 终点 + 夹爪变化；全程不动记为保持）。

        只摘要位姿与夹爪（与下发给模型的 state 同口径，不含关节）；比回灌模型原始输出更短、
        也不会被截半（每个臂一段，长度只随臂数增长）。
        """
        entries = []
        for index, arm in enumerate(self._arms):
            start = index * ACTION_DIM_PER_ARM
            segment = block[:, start : start + ACTION_DIM_PER_ARM]
            if segment.shape[1] != ACTION_DIM_PER_ARM:
                continue
            head, tail = segment[0], segment[-1]
            if float(np.max(np.abs(segment - head))) <= 1e-6:  # 整块不动（未提及的臂 / 保持）
                entries.append(f"{arm} held at ({head[0]:.3f}, {head[1]:.3f}, {head[2]:.3f}), gripper {head[6]:.2f}")
                continue
            entries.append(
                f"{arm} ({head[0]:.3f}, {head[1]:.3f}, {head[2]:.3f}) -> "
                f"({tail[0]:.3f}, {tail[1]:.3f}, {tail[2]:.3f}), "
                f"gripper {head[6]:.2f} -> {tail[6]:.2f}"
            )
        return "; ".join(entries)

    def config_snapshot(self) -> dict:
        """当前生效配置（供状态上报 / 调试；**密钥只报来源，不回显值**）。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "horizon": self.horizon,
            "image_size": self.image_size,
            "history_len": self.history_len,
            "max_points": self.max_points,
            "max_pose_step": self.max_pose_step,
            "arms": self.arms,
            "api_key_env": self.api_key_env,
            "api_key_present": self._api_key() is not None,
            "api_key_source": self._api_key_source(),
        }

    def status(self) -> dict:
        """策略状态（server /v1/infers 上报用）。"""
        return {
            "type": "llm",
            "connected": self.connected,
            "prompt": self.prompt,
            "history_len": len(self._history),
            "config": self.config_snapshot(),
        }


__all__ = ["LLMPolicyClient"]
