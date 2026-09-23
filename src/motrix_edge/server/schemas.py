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

"""server 请求 / 响应模型（Pydantic）—— 各路由的入参契约单点。

与 ``routes/*`` 的 HTTP 映射分离：本模块只描述线上形状（字段 / 校验 / 描述文本），
不含任何业务逻辑与依赖注入，便于被路由、测试与文档引用。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from motrix_edge.lease import LeaseState


class CommandRequest(BaseModel):
    """POST /v1/commands 请求体（契约：lease_id / command_id / capability / 参数 / 现场在场）。

    ``idempotency_key``：**预留、未实现** —— 幂等去重尚未落地（见 server/command.py），
    字段仅作调用方关联回显；调用方需自行处理重试，勿依赖去重。
    """

    command_id: str = Field(..., description="Edge 侧命令 ID")
    lease_id: str | None = None
    capability: str | None = Field(
        default=None,
        description=(
            "操作能力，命名 <scope>/<verb>（如 robot/execute）；"
            "scope = robot / capture / infer / node / lease / adapter"
        ),
    )
    params: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, description="预留：幂等未实现，仅回显关联")
    onsite_presence_context: str | None = None


class CommandResponse(BaseModel):
    command_id: str
    # 命令回执状态：ok（执行成功）/ rejected（业务拒绝，如参数非法 / 状态不符）/ error /
    # accepted（push 型命令：已入总线，无回执；未被 CommandService 消费时的骨架）
    status: str
    idempotency_key: str | None
    correlation_id: str
    # 命令执行结果（CommandService 返回透传；push 型命令为 None）：
    # executed=执行的 capability（规范拼写）/ error=执行错误（如 robot/execute 维度不符）
    executed: str | None = None
    error: str | None = None
    data: dict | None = None
    # edge 错误码（拒绝时给出，见 motrix_edge.errors.ErrorCode）；成功 / push 型为 null
    code: str | None = None
    # 请求侧用了旧 capability 拼写（robot_execute 一类）→ true，提示调用方迁移
    deprecated: bool = Field(default=False, description="请求使用了旧 capability 拼写")


class LeaseInstallRequest(BaseModel):
    """POST /v1/leases 请求体：Console 签发并下发的租约**镜像**（权威在 Console）。

    字段见 wiki/design/motrix_edge_lease.md「lease 信息」：lease_id / edge_id /
    holder_subject_id / purpose / state / expires_at / lease_version；``ttl`` 为
    有效期（秒，信息字段）。``expires_at`` 由 Console 决定并随镜像下发。
    """

    lease_id: str = Field(..., description="Console 生成的租约 id")
    edge_id: str = Field(..., description="租约所属 edge 设备")
    holder_subject_id: str = Field(..., description="租约所属操作员")
    purpose: str = Field(..., description="租约用途（如 capture / rollout / maintenance）")
    state: LeaseState = Field(default=LeaseState.ACTIVE, description="签发状态（reserved / active）")
    expires_at: datetime = Field(..., description="过期时间（ISO 8601，北京时间；Console 决定）")
    lease_version: int = Field(default=1, ge=1, description="租约版本；续约时递增")
    ttl: float | None = Field(default=None, gt=0, description="有效期（秒，信息字段）")


class LeaseRenewRequest(BaseModel):
    """POST /v1/leases/{id}:renew 请求体：Console 续约 —— 更高 lease_version + 新 expires_at。"""

    lease_version: int = Field(..., ge=1, description="新租约版本（须高于当前，版本回退拒绝）")
    expires_at: datetime = Field(..., description="续约后的过期时间（ISO 8601，北京时间；Console 决定）")


class WebRTCOfferRequest(BaseModel):
    """POST /v1/webrtc/offer 请求体：网页 SDP offer。"""

    sdp: str = Field(..., description="网页端 SDP offer")
    type: str = Field(default="offer", description="SDP 类型（offer）")


class InferEnterRequest(BaseModel):
    """POST /v1/infers 请求体：可选推理策略类型 + 进入会话前的策略配置项。

    ``config`` 为**该策略的配置项**（如需端点的策略含端点项 ``host`` / ``port``，即 openpi /
    lerobot-act；openpi → prompt；
    lerobot-act → pretrained_name_or_path / device / actions_per_chunk），见
    ``policy.POLICY_CONFIG_ITEMS``；前端按 ``/v1/health`` 的 ``policy_config_items``
    （或在会话内按 ``GET /v1/infers`` 的 ``policy_config.items``）动态渲染表单，提交时随本字段下发。
    """

    policy_type: str | None = Field(default=None, description="推理策略类型（注册表键），如 openpi")
    config: dict | None = Field(
        default=None, description="策略配置项（按所选策略 schema 校验；需端点的策略含 host / port）"
    )


class InferRolloutRequest(BaseModel):
    """POST /v1/infers/rollout 请求体：推理模式（single 单步 / continuous 持续）。

    多步（count）与 drain（缓存推理）模式已取消（多余字段被忽略）；prompt 不随 rollout 传，
    由会话内 ``infer prompt`` 预置（为空不能开始推理 / 录制）。
    """

    mode: Literal["single", "continuous"] | None = Field(
        default=None, description="推理模式：single（缺省）/ continuous"
    )


class InferPromptRequest(BaseModel):
    """POST /v1/infers/prompt 请求体：文本指令（仅需要 prompt 的策略，如 openpi）。"""

    prompt: str = Field(..., min_length=1, description="文本指令（需要 prompt 的策略推理前必须设置）")


class InferSyncRequest(BaseModel):
    """POST /v1/infers/sync 请求体：录制 rollout 时同步的采集元信息（operator / task_name 等）。"""

    meta: dict = Field(default_factory=dict, description="采集元信息（默认 operator=policy、task_name=prompt）")


class InferRTCRequest(BaseModel):
    """POST /v1/infers/rtc 请求体：RTC（实时动作块）参数（可部分更新）。

    对应命令 ``infer rtc set <json>``；参数写入内存态 ``policy.rtc`` 并应用到正在运行的
    RTCManager（下一块起生效）；见 wiki/design/motrix_edge_rtc.md。
    """

    enabled: bool | None = Field(default=None, description="是否启用 RTC（关闭 → 每步一次推理只取块首步）")
    action_horizon: int | None = Field(default=None, ge=1, description="块长上限 H（一次推理只取块的前 H 步）")
    prefix_len: int | None = Field(
        default=None, ge=0, description="前置段 P（额外强制跳过的前 P 步；真实过期步自动跳过）"
    )
    execution_horizon: int | None = Field(default=None, ge=1, description="执行段 E（缺省 = H - P - S）；须 > P")
    suffix_len: int | None = Field(
        default=None, ge=0, description="后缀段 S（与下一块执行段的重叠窗口，也是预取提前量）；须满足 P + S < H"
    )
    aggregate_fn: str | None = Field(
        default=None,
        description=(
            "重叠过渡策略：weighted_average(0.3 本段+0.7 下一段)/conservative(0.7+0.3)/"
            "average/latest_only/continuous(按步号 0→1 线性过渡)"
        ),
    )


class InferConfigRequest(BaseModel):
    """POST /v1/infers/config 请求体：策略配置项（可部分更新）。

    对应命令 ``infer config set <json>``；按**当前策略**的配置项 schema 白名单校验并写入
    内存态 ``policy`` 段：``runtime: True`` 的键立即应用到运行中的策略客户端（下一请求生效），
    ``runtime: False`` 的键（host / port、模型路径…）要退出会话重进才生效，回执 ``deferred``
    列出这些键。未知键 / 类型不符 / 必填为空 → 400；空值（``null`` / 空串）表示清除该项。
    仅需 prompt 的策略也可用 ``POST /v1/infers/prompt`` 快捷入口。
    """

    config: dict = Field(default_factory=dict, description="策略配置项（按当前策略 schema 校验）")


class UploadScanRequest(BaseModel):
    """POST /v1/uploads 请求体：可覆盖配置的默认采集目录。"""

    folder_path: str | None = Field(default=None, description="待扫描目录；缺省使用 upload.data_dir")


class CaptureSyncRequest(BaseModel):
    """POST /v1/captures/sync 请求体：采集元信息（采集员 / 任务名等，进程保存数据时附加）。"""

    meta: dict = Field(default_factory=dict, description="采集元信息（operator / task_name 等）")


class AdapterConfigRequest(BaseModel):
    """POST /v1/adapters/config 请求体：运行时 adapter 能力配置（可部分更新）。"""

    enabled_arms: list[str] | None = Field(default=None, description="启用的机械臂（right / left）；缺省全部")
    enabled_cameras: list[str] | None = Field(default=None, description="启用的相机（IMAGES 子集）")


class UploadSelectRequest(BaseModel):
    """POST /v1/uploads/select 请求体：按 episode id 替换选择集。"""

    episode_ids: list[str] = Field(default_factory=list)


class UploadPackRequest(BaseModel):
    """POST /v1/uploads/pack 请求体：打包（移动）选中 episode 的包名。

    包目录建在当前扫描目录下（``<folder_path>/<name>/``）；缺省 ``pack<选中数量>``；
    目录同名已存在 → 409（需改名）；见 wiki/design/motrix_edge_upload_session.md。
    """

    name: str | None = Field(default=None, description="包名（单个目录名）；缺省 pack<选中数量>")


class CaptureMetaAddRequest(BaseModel):
    """POST /v1/captures/meta 请求体：新增采集元信息选项（分类不存在则自动创建）。"""

    key: str = Field(..., description="分类（如 operator / task_name）")
    value: str = Field(..., description="选项值")


class CaptureMetaEditRequest(BaseModel):
    """PATCH /v1/captures/meta 请求体：重命名采集元信息选项（``old`` → ``new``）。"""

    key: str = Field(..., description="分类")
    old: str = Field(..., description="原选项值")
    new: str = Field(..., description="新选项值")


__all__ = [
    "AdapterConfigRequest",
    "CaptureMetaAddRequest",
    "CaptureMetaEditRequest",
    "CaptureSyncRequest",
    "CommandRequest",
    "CommandResponse",
    "InferConfigRequest",
    "InferEnterRequest",
    "InferPromptRequest",
    "InferRTCRequest",
    "InferRolloutRequest",
    "InferSyncRequest",
    "LeaseInstallRequest",
    "LeaseRenewRequest",
    "UploadPackRequest",
    "UploadScanRequest",
    "UploadSelectRequest",
    "WebRTCOfferRequest",
]
