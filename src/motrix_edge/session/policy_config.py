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

"""策略配置功能 —— 会话共享的「策略配置项」命令面 + prompt 门控。

``infer``（推理）与 ``rl``（残差 RL）都持有策略客户端（``self.policy``）与运行时策略类型
（``self.policy_type``），故两者都装配本功能（mixin，放在 ``BaseSession`` 之前，从而可用基座的
``_reply`` / ``state``）：

- **config 面**：``infer config`` / ``infer config set <json>`` / ``infer model`` /
  ``infer model set <path>`` —— 按当前策略 schema 校验后写内存态 ``base_cfg["policy"]``
  （不写回 yaml）；``runtime=True`` 的键同时应用到运行中的策略客户端（下一请求生效），其余进
  回执 ``deferred`` 提示需退出会话重进；
- **prompt 门控**：语言条件策略（``policy.requires_prompt``，如 openpi）在推理 / 开录前 prompt
  必须非空（prompt 同时是录制 episode 的 ``task_name``）；非语言条件策略（lerobot-act）不参与门控。

单一事实来源 = 内存态 ``base_cfg["policy"]["prompt"]``；策略客户端的 ``prompt`` 只是运行时镜像。
"""

from motrix_edge.command import (
    CMD_INFER_CONFIG,
    CMD_INFER_CONFIG_SET,
    CMD_INFER_MODEL,
    CMD_INFER_MODEL_SET,
    CommandResult,
    handle_policy_config,
    ok_result,
    policy_config_status,
)
from motrix_edge.errors import ErrorCode
from motrix_edge.policy import policy_config_runtime_keys, validate_policy_type
from motrix_edge.utils.data_handler import debug_print

#: 策略配置命令族：``infer config`` / ``infer config set <json>`` / ``infer model`` / ``infer model set``
POLICY_CONFIG_COMMANDS = (
    CMD_INFER_CONFIG,
    CMD_INFER_CONFIG_SET,
    CMD_INFER_MODEL,
    CMD_INFER_MODEL_SET,
)


class PolicyConfigFeature:
    """策略配置功能（mixin）：config 命令面 + runtime 热改 + prompt 门控。"""

    # ---- 只读状态（快照 / 门控消费）-----------------------------------------
    @property
    def prompt(self) -> str | None:
        """当前推理文本指令（策略客户端 prompt；未设置为 None）。

        仅语言条件策略（``requires_prompt=True``，如 openpi）必需；lerobot-act 等非语言条件策略
        不需要（保持 None，不参与门控、不下发）。
        """
        return getattr(getattr(self, "policy", None), "prompt", None)

    @property
    def prompt_required(self) -> bool:
        """当前策略是否需要 prompt（委托 ``policy.requires_prompt``）。"""
        return bool(getattr(getattr(self, "policy", None), "requires_prompt", False))

    def policy_config_status(self) -> dict:
        """策略配置项状态（schema + 当前值 + 缺失必填项；server ``/v1/infers`` 上报 / 前端表单）。"""
        return policy_config_status(self.base_cfg, policy_type=self.policy_type)

    # ---- 命令面 -------------------------------------------------------------
    def dispatch_policy_config(self, name, cmd) -> bool:
        """策略配置命令族分发（``infer config(set)`` / ``infer model(set)``）；返回 True = 已回执。

        会话主循环与步进循环都调它（``infer rollout`` 持续中 / ``rl rollout`` 闭环内同样可改配置）。
        """
        if name not in POLICY_CONFIG_COMMANDS:
            return False
        self._reply(cmd, self._on_policy_config(cmd))
        return True

    def _on_policy_config(self, cmd):
        """策略配置命令族：``infer config`` / ``infer config set <json>`` /
        ``infer model(set)``（**端点项 host / port（仅需端点的策略）+ 每个策略自己的配置项**，
        见 ``policy.POLICY_CONFIG_ITEMS``）。

        **所有配置项一视同仁**——推理端点 host / port（openpi / lerobot-act）与 prompt / 模型路径 /
        动作块长度走同一 schema、同一校验、同一通道，**没有「连接后锁定」这一额外轴**：

        - 先经 ``handle_policy_config`` 校验并写入内存态 ``base_cfg["policy"]``（下次会话生效）；
        - 设置类命令再按 **``runtime``** 决定是否即时应用：``runtime=True``（prompt / image_size，
          策略每次请求现读）→ 应用到运行中的客户端，下一请求生效；``runtime=False``（host / port、
          lerobot-act 的模型路径 / device / 动作块长度——**进入会话时固化的握手级配置**）→ 只写
          内存态配置，回执里以 ``deferred`` 告知需退出会话重进。

        参数缺失 / 非法键 / 类型不符 / 越界 → rejected（400，不崩溃）。
        """
        result = handle_policy_config(self.base_cfg, cmd, policy_type=self.policy_type)
        if result.status != "ok":
            return result
        written = result.data.get("written") or {}
        deferred = self._apply_policy_config(written) if written else []
        extra = {"deferred": deferred}  # 恒有该键（可能为空列表）：回执形状稳定，调用方无需防缺键
        return ok_result(state=getattr(self, "state", "ready"), **(result.data or {}), **extra)

    # ---- prompt 门控 --------------------------------------------------------
    def _require_prompt(self, cmd) -> bool:
        """推理 / 回合开录前的门控：**仅对需要 prompt 的策略**（``policy.requires_prompt``）。

        语言条件策略（openpi）要求已预置非空文本（策略配置项 prompt，经 ``infer config set``
        写入）——空 → 回执 rejected（400）并返回 False（不执行推理 / 不开录制）；prompt 同时作为录制
        episode 的 ``task_name``。非语言条件策略（lerobot-act：ACT 不接受文本条件）**不需要 prompt**，
        不门控、直接放行。
        """
        if not self.prompt_required:
            return True
        prompt = self.prompt
        if not prompt or not str(prompt).strip():
            self._reply(
                cmd,
                CommandResult(
                    status="rejected",
                    error="prompt required: set policy config 'prompt' before inference / recording",
                    code=ErrorCode.INVALID_ARGUMENT,
                ),
            )
            return False
        return True

    # ---- 运行时应用（runtime 轴）--------------------------------------------
    def _effective_policy_type(self) -> str:
        """本会话实际使用的策略类型（显式选择优先，否则配置 ``policy.type``；非法 → 空串）。"""
        try:
            return validate_policy_type(self.policy_type or self.policy_config.get("type", "openpi"))
        except ValueError:
            return ""

    def _apply_prompt(self, prompt) -> None:
        """应用推理文本指令（语言条件策略的配置项）：写入策略客户端运行时 ``prompt``。

        openpi 每次 infer 请求携带该文本（服务端每帧重新 tokenize，可换）；不声明 prompt
        配置项的策略（如 lerobot-act）无 ``prompt`` 属性，此处 no-op。prompt 是普通策略配置项
        （经 ``infer config set`` / ``POST /v1/infers/config`` 写入，不随 rollout 命令传）；
        需要 prompt 的策略在推理 / 录制开始前必须非空。
        ``prompt`` 非 None 即设置（空文本已在调用方校验）。
        """
        if prompt is None:
            return
        policy = getattr(self, "policy", None)
        if policy is not None and hasattr(policy, "prompt"):
            policy.prompt = str(prompt)
            debug_print(self.name, f"Policy prompt set: {prompt!r}", "INFO")

    def _apply_policy_config(self, written: dict) -> list[str]:
        """把**会话内可热改**的配置项应用到运行中的策略客户端（下一请求生效）。

        只有 ``runtime=True`` 的键即时生效：``prompt`` → 策略 prompt（语言条件策略每次请求携带）；
        其余键 → ``policy.policy_config``（策略自读，如 openpi 的 ``image_size`` 每次请求现读）。
        ``runtime=False`` 的键（host / port、lerobot-act 的模型路径 / device / 动作块长度）在**进入
        会话时**已固化到策略客户端与传输层，改内存态配置需退出会话重进才生效。

        返回本次写入但**未**即时生效（延后到下次会话）的键，供回执 ``deferred`` 提示。
        """
        runtime_keys = policy_config_runtime_keys(self._effective_policy_type())
        live = {key: value for key, value in written.items() if key in runtime_keys}
        if live.get("prompt") is not None:
            self._apply_prompt(live.pop("prompt"))
        policy_config = getattr(getattr(self, "policy", None), "policy_config", None)
        if isinstance(policy_config, dict):
            for key, value in live.items():
                if value is None:  # 清除项：删键让运行中的客户端回退代码缺省
                    policy_config.pop(key, None)
                else:
                    policy_config[key] = value
        return sorted(key for key in written if key not in runtime_keys)
