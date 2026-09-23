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

"""routes.infers —— ``/v1/infers/*`` 的 HTTP 映射（推理会话控制）。

无回合概念：``enter`` → ``rollout``（单步 / 持续）→ ``exit``；录制由 ``episode/start·end``
（= ``capture episode start/end``）、元信息由 ``sync``（= ``capture sync``）。每个动作都映射为
**一条命令**（经 ``CommandService.submit``，由推理会话消费）——与 CLI 逐条对应，路由不做业务
判断；``GET /v1/infers`` 是只读快照（``server/status.py``）。未注入 → 501。

``enter`` 的 ``policy_type`` / ``config`` 随 ``session run infer`` 的命令参数下发，由节点在
实例化会话**之前**应用——同一逻辑也服务 CLI（``session run infer config='{"prompt":"..."}'``）。
"""

from fastapi import APIRouter, Header

from motrix_edge.command import (
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_EPISODE_START,
    CMD_CAPTURE_SYNC,
    CMD_INFER_CONFIG_SET,
    CMD_INFER_CONNECT,
    CMD_INFER_PROMPT,
    CMD_INFER_ROLLOUT,
    CMD_INFER_ROLLOUT_STOP,
    CMD_INFER_RTC_SET,
    CMD_SESSION_QUIT,
    CMD_SESSION_RUN,
)
from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.server.deps import Services
from motrix_edge.server.routes._common import accepted
from motrix_edge.server.schemas import (
    InferConfigRequest,
    InferEnterRequest,
    InferPromptRequest,
    InferRolloutRequest,
    InferRTCRequest,
    InferSyncRequest,
)
from motrix_edge.server.status import infer_snapshot


def build_router(services: Services) -> APIRouter:
    """构建推理会话路由。"""
    router = APIRouter()
    node = services.node
    commands = services.commands
    leases = services.leases

    def _node():
        if node is None:
            raise ServiceError("infers not enabled (node not injected)", code=ErrorCode.NOT_IMPLEMENTED)
        return node

    def _commands():
        if commands is None:
            raise ServiceError("infers not enabled (CommandService not injected)", code=ErrorCode.NOT_IMPLEMENTED)
        return commands

    @router.get("/v1/infers")
    def infers_status():
        """状态快照：node_state / session_type / state / adapter / policy / connected / metadata /
        prompt / prompt_required / warmed_up / warming / warmup_error / dropped_actions / recording /
        continuous / capture_status / rtc / policy_config / lease_id。"""
        return infer_snapshot(_node(), leases)

    @router.post("/v1/infers")
    def infers_enter(req: InferEnterRequest | None = None, x_lease_id: str | None = Header(default=None)):
        """进入推理会话（READY → ACTIVE）：连接推理会话并启动任务循环，需先持有有效租约。

        请求体可选：``policy_type`` 指定推理策略（缺省用配置 policy.type）、``config`` 指定
        **该策略的配置项**（如需端点的策略含端点项 ``host`` / ``port``，如 lerobot-act 的模型路径 /
        openpi 的 prompt）；非法键、类型不符、越界或必填项为空 → 400。
        """
        policy_type = req.policy_type if req is not None else None
        config = req.config if req is not None else None
        params: dict = {"session": "infer"}
        if policy_type:
            params["policy_type"] = policy_type
        if config:
            # 进入会话前的策略配置项：随命令下发，由节点在实例化会话前应用（node._on_ready）
            params["config"] = dict(config)
        return accepted(_commands().submit(CMD_SESSION_RUN, params, lease_id=x_lease_id))

    @router.post("/v1/infers/connect")
    def infers_connect(x_lease_id: str | None = Header(default=None)):
        """启动 / 查询**异步预热**（infer connect）。须已在推理会话且持有租约。

        立即回执（`started` / `warming` / `warmed_up` / `warmup_error`，重复调用幂等）：预热可能
        持续几十秒～几分钟（加载 checkpoint），进度由 `GET /v1/infers` 轮询；预热**不下发动作**，
        且可被急停（`POST /v1/commands` capability=robot/estop）或退出会话立即中断。
        """
        return accepted(_commands().submit(CMD_INFER_CONNECT, lease_id=x_lease_id))

    @router.post("/v1/infers/rollout")
    def infers_rollout(req: InferRolloutRequest | None = None, x_lease_id: str | None = Header(default=None)):
        """推理闭环（infer rollout）：单步（缺省）或 continuous 持续。

        body：``mode``（single 缺省 / continuous）。
        需要 prompt 的策略（如 openpi）不随 rollout 传 prompt（会话内 ``infer prompt`` 预置）；lerobot-act 不需要。
        须已在推理会话且持有租约；continuous 启动即回执 started，直到 ``infer rollout stop`` /
        session quit / estop。
        """
        mode = req.mode if req is not None else None
        return accepted(_commands().submit(CMD_INFER_ROLLOUT, {"mode": mode}, lease_id=x_lease_id))

    @router.post("/v1/infers/rollout/stop")
    def infers_rollout_stop(x_lease_id: str | None = Header(default=None)):
        """停止持续推理（``infer rollout stop``）：回到会话 READY，**不退会话、不断策略连接**。

        与 ``DELETE /v1/infers``（session quit）的区别：会话与策略连接保留，仍可再次
        ``infer rollout`` / 改配置。未在持续推理中 → 409。受控操作：须持有租约。
        """
        data = _commands().submit(CMD_INFER_ROLLOUT_STOP, lease_id=x_lease_id)
        return {**accepted(data), "continuous": False}  # 会话回执不含该位，按语义补（前端据此复位按钮）

    @router.post("/v1/infers/episode/start")
    def infers_episode_start(x_lease_id: str | None = Header(default=None)):
        """开始一轮推理 rollout 录制（capture episode start）：robot 开始录 mcap（含 action）。

        需要 prompt 的策略（如 openpi）：prompt 为空 → 400（先 ``infer prompt`` 预置）；lerobot-act 不需要。
        录制前由调用方 ``POST /v1/infers/sync`` 显式同步采集元信息（默认 operator=policy、
        task_name=prompt）。受控操作：须持有租约。
        """
        return accepted(_commands().submit(CMD_CAPTURE_EPISODE_START, lease_id=x_lease_id))

    @router.post("/v1/infers/episode/end")
    def infers_episode_end(x_lease_id: str | None = Header(default=None)):
        """结束一轮推理 rollout 录制（capture episode end）：robot 保存该 episode。受控操作。"""
        return accepted(_commands().submit(CMD_CAPTURE_EPISODE_END, lease_id=x_lease_id))

    @router.post("/v1/infers/sync")
    def infers_sync(req: InferSyncRequest, x_lease_id: str | None = Header(default=None)):
        """同步采集元信息（capture sync）：录制 rollout 时把 operator/task_name 同步到进程。

        默认元信息 = ``{operator: "policy", task_name: <prompt>}``，**由调用方自行组装并显式**
        提交本端点（Edge 不自动 sync；也不在状态里代报默认值）。受控操作：须持有租约。
        """
        return accepted(_commands().submit(CMD_CAPTURE_SYNC, {"meta": req.meta}, lease_id=x_lease_id))

    @router.post("/v1/infers/rtc")
    def infers_rtc(req: InferRTCRequest, x_lease_id: str | None = Header(default=None)):
        """运行期设置 RTC（实时动作块）参数（``infer rtc set``）。

        body 为参数对象（可部分：enabled / action_horizon / prefix_len / execution_horizon /
        suffix_len / aggregate_fn）→ 写入内存态 ``policy.rtc`` 并应用到正在运行的
        RTCManager（下一块起生效）；非法参数或违反交叉约束（P + S < H、E > P）→ 400。
        受控操作：须持有租约。
        """
        params = {key: value for key, value in req.model_dump().items() if value is not None}
        return accepted(_commands().submit(CMD_INFER_RTC_SET, {"json": params}, lease_id=x_lease_id))

    @router.post("/v1/infers/config")
    def infers_config(req: InferConfigRequest, x_lease_id: str | None = Header(default=None)):
        """运行期设置**策略配置项**（``infer config set``）。

        body 为配置项对象（可部分：openpi → prompt；lerobot-act → pretrained_name_or_path / device /
        actions_per_chunk）→ 按当前策略 schema 白名单校验并写入内存态 ``policy`` 段：
        ``runtime: True`` 的键同样应用到运行中的策略客户端（下一请求生效），``runtime: False``
        的键（host / port、模型路径…）退出会话重进才生效并在回执 ``deferred`` 列出。
        未知键 / 类型不符 / 必填为空 → 400。受控操作：须已在推理会话且持有租约。
        """
        return accepted(_commands().submit(CMD_INFER_CONFIG_SET, {"json": dict(req.config)}, lease_id=x_lease_id))

    @router.post("/v1/infers/prompt")
    def infers_prompt(req: InferPromptRequest, x_lease_id: str | None = Header(default=None)):
        """会话内预置/更新推理文本指令（统一 prompt；推理/录制前必须非空）。

        须已在推理会话且持有租约；持续推理中亦可修改（下个请求生效）。仅对声明 prompt
        配置项的策略（语言条件，如 openpi）有效；等价于 ``POST /v1/infers/config``
        提交 ``{"prompt": ...}``。
        """
        if not req.prompt.strip():  # 纯空白不算指令（pydantic 的 min_length 只拦空串）
            raise ServiceError("prompt required", code=ErrorCode.INVALID_ARGUMENT)
        return accepted(_commands().submit(CMD_INFER_PROMPT, {"prompt": req.prompt}, lease_id=x_lease_id))

    @router.delete("/v1/infers")
    def infers_exit(lease_id: str | None = None):
        """退出推理会话（ACTIVE → READY）。租约经 query 参数 `lease_id` 提交校验。"""
        # session quit 退出任务：节点在任务结束后补发「node ready」回执（超时给足）
        return accepted(_commands().submit(CMD_SESSION_QUIT, lease_id=lease_id, timeout=10.0))

    return router
