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

"""Test LLM Point —— 独立运行的模拟 LLM/VLM 策略端点（OpenAI 兼容，虚拟云端模型）。

**不进行真实推理**：作为联调用虚拟模型端点，验证「edge ``policy.type=llm`` → 云端
OpenAI 兼容 API」的整条链路（见 wiki/design/motrix_edge_llm_policy.md）：

- ``GET  /v1/models``：策略 ``prepare()`` 的端点 / 密钥探测；
- ``POST /v1/chat/completions``：接收 edge 的观测消息（文本状态 + base64 图像），从文本里
  解析各臂当前位姿，返回**朝着目标位姿逐点前进的笛卡尔轨迹** JSON（``trajectory``）——
  位姿越接近目标，单块位移越小、夹爪越接近闭合，因此在真实闭环下会**收敛**。

与 Edge 的耦合**仅限模型输出契约**（轨迹 JSON 的字段名），不 import ``motrix_edge``；
既可独立运行（进程入口），也可被测试进程内复用（``create_app``）。

独立运行::

    uv run python scripts/test_llm_point.py [--host 0.0.0.0] [--port 8080] \
        [--target 0.3 0.0 0.15] [--arm left] [--points 8] [--dt 0.6] [--step 0.03]
"""

from __future__ import annotations

import argparse
import json
import re
import time

import uvicorn
from fastapi import FastAPI

# 默认参数（SimLLMCore 类常量与此对齐）
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_TARGET = (0.30, 0.0, 0.15)  # 目标末端位置（米，机器人基座坐标系）
DEFAULT_POINTS = 8  # 每次回复的轨迹点数
DEFAULT_DT = 0.6  # 轨迹点时间间隔（秒）
DEFAULT_STEP = 0.03  # 每个轨迹点最大前进距离（米）
DEFAULT_CLOSE_DISTANCE = 0.05  # 距目标小于该距离（米）→ 闭合夹爪
DEFAULT_ARMS = ["left", "right"]

# 从 edge 的用户消息里解析臂名与其当前位姿：``- left: pose_pos=[0.300, 0.000, 0.150] ...``
_ARM_LINE = re.compile(r"^-\s*(?P<arm>[\w\-]+):\s*pose_pos=\[(?P<pos>[^\]]*)\]", re.MULTILINE)


class SimLLMCore:
    """模拟 LLM 核心：按「当前位姿 → 目标位姿」生成笛卡尔轨迹（无真实模型）。

    ``reply(messages)``：从最后一条用户消息解析各臂位姿，输出 OpenAI 兼容的 chat completion
    响应；``trajectory`` 由指定臂的 ``points`` 个等时间隔点组成，每点朝目标前进 ``step``
    （距目标足够近时直接落到目标并闭合夹爪）。未指定的臂不出现在轨迹里（edge 侧保持不动）。
    """

    MODEL = "test-llm-point"  # 虚拟模型名（/v1/models 与响应体）

    def __init__(
        self,
        target: tuple[float, float, float] = DEFAULT_TARGET,
        arm: str | None = None,
        arms: list[str] | None = None,
        points: int = DEFAULT_POINTS,
        dt: float = DEFAULT_DT,
        step: float = DEFAULT_STEP,
        close_distance: float = DEFAULT_CLOSE_DISTANCE,
    ):
        self.target = tuple(float(v) for v in target)
        self.arms = [str(a) for a in (arms or DEFAULT_ARMS)]
        self.arm = str(arm) if arm else None  # None → 用观测里出现的第一个臂
        self.points = max(1, int(points))
        self.dt = float(dt)
        self.step = float(step)
        self.close_distance = float(close_distance)
        self.requests = 0
        self.last_messages: list = []

    # ---- 观测解析 -------------------------------------------------------------
    @staticmethod
    def parse_poses(text: str) -> dict[str, list[float]]:
        """从用户消息文本解析 ``{臂名: [x, y, z]}``（解析失败 / 缺失 → 空 dict）。"""
        poses: dict[str, list[float]] = {}
        for match in _ARM_LINE.finditer(str(text or "")):
            try:
                values = [float(v) for v in match.group("pos").replace(" ", "").split(",") if v]
            except ValueError:
                continue
            if len(values) == 3:
                poses[match.group("arm")] = values
        return poses

    # ---- 轨迹生成 -------------------------------------------------------------
    def trajectory(self, poses: dict[str, list[float]]) -> list[dict]:
        """生成朝着目标收敛的稀疏轨迹（单臂；未解析到位姿时从目标附近开始）。

        每个轨迹点最多朝目标前进 ``step`` 米（半径收敛）；距目标足够近时直接落到目标并闭合
        夹爪——真机闭环下这意味着「每次推理都往前走一段、越近越慢」。
        """
        arm = self.arm or (next(iter(poses)) if poses else self.arms[0])
        pos = [float(v) for v in (poses.get(arm) or self.target)]
        points = []
        for index in range(self.points):
            delta = [self.target[axis] - pos[axis] for axis in range(3)]
            distance = float(sum(value * value for value in delta) ** 0.5)
            if distance <= self.step:
                pos = list(self.target)
            else:
                ratio = self.step / distance
                pos = [pos[axis] + delta[axis] * ratio for axis in range(3)]
            points.append(
                {
                    "t": round(index * self.dt, 3),
                    "arm": arm,
                    "pos": [round(value, 6) for value in pos],
                    "rot": [3.14, 0.0, 0.0],  # 固定姿态（末端朝下）
                    "gripper": 1.0 if distance <= self.close_distance else 0.0,
                }
            )
        return points

    def reply(self, body: dict) -> dict:
        """OpenAI 兼容 chat completion 响应：内容为含 ``trajectory`` 的 JSON 字符串。"""
        self.requests += 1
        messages = list((body or {}).get("messages") or [])
        self.last_messages = messages
        text = " ".join(str(self._content(message)) for message in messages if message.get("role") == "user")
        payload = {
            "reasoning": "move the arm toward the target pose",
            "trajectory": self.trajectory(self.parse_poses(text)),
        }
        return {
            "id": f"chatcmpl-test-{self.requests}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(payload)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    @staticmethod
    def _content(message: dict):
        """消息内容 → 文本（兼容纯字符串与内容块数组两种形态）。"""
        content = (message or {}).get("content")
        if isinstance(content, list):
            return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return content or ""


def create_app(core: SimLLMCore) -> FastAPI:
    """组装 OpenAI 兼容的模拟模型端点（``/v1/models`` + ``/v1/chat/completions``）。"""
    app = FastAPI(title="Test LLM Point")

    @app.get("/v1/models")
    def models():
        """模型列表：策略 ``prepare()`` 的端点 / 密钥探测（不消耗 token）。"""
        return {"object": "list", "data": [{"id": core.MODEL, "object": "model", "owned_by": "test"}]}

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict):
        """对话补全：返回脚本化的笛卡尔轨迹（JSON 字符串，含 ``` 包裹以校验解析健壮性）。"""
        response = core.reply(body)
        response["choices"][0]["message"]["content"] = (
            "```json\n" + response["choices"][0]["message"]["content"] + "\n```"
        )
        return response

    return app


def main() -> None:
    """进程入口：启动模拟 LLM 端点（可被 edge 作为 ``policy.base_url`` 指向）。"""
    parser = argparse.ArgumentParser(description="Test LLM Point —— 模拟云端 LLM 策略端点")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--target", type=float, nargs=3, default=list(DEFAULT_TARGET), help="目标末端位置 xyz（米）")
    parser.add_argument("--arm", default=None, help="被控制的臂名（缺省 = 观测里出现的第一个臂）")
    parser.add_argument("--points", type=int, default=DEFAULT_POINTS, help="每次回复的轨迹点数")
    parser.add_argument("--dt", type=float, default=DEFAULT_DT, help="轨迹点时间间隔（秒）")
    parser.add_argument("--step", type=float, default=DEFAULT_STEP, help="每个轨迹点最大前进距离（米）")
    args = parser.parse_args()

    core = SimLLMCore(target=tuple(args.target), arm=args.arm, points=args.points, dt=args.dt, step=args.step)
    uvicorn.run(create_app(core), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
