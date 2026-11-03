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

"""policy 包 —— 推理策略客户端：注册式工厂 + 懒加载。

与 adapter / session 包一致：通过 POLICY_REGISTRY 注册策略客户端，
由 get_policy(base_cfg) 依据配置 policy.type 选择性实例化。

策略客户端 = 传输（openpi → ``transport`` 的 ws+msgpack；lerobot-remote → vendored ``src/lerobot``
            的 zenoh 客户端，自带协议） + 格式契约（contract.py）+ 策略特有行为（openpi/、lerobot_remote/）。
            策略**只负责取推理结果**
            （``infer_chunk``）；动作块缓存 / 三元切分 / 时序平滑 / 预取时机由 ``motrix_edge.rtc``
            统一负责（见 wiki/design/motrix_edge_rtc.md）。
第三方依赖（websockets / grpc / vendored lerobot）均为懒加载，避免导入 motrix_edge
时因缺依赖报错。
"""

import importlib

# 注册表：策略类型名 -> (模块路径, 类名)
# 懒加载：仅当 get_policy() 选中该类型时才 import 对应模块（连带加载其第三方依赖）。
POLICY_REGISTRY = {
    "openpi": ("motrix_edge.policy.openpi.client", "OpenPIClient"),
    "lerobot-remote": ("motrix_edge.policy.lerobot_remote.client", "LerobotRemoteClient"),
}

# 策略配置项（**静态声明**：每个策略有自己的独立配置项——prompt / 模型路径 / 设备 / 块长…）。
# 供 CLI（`infer config`）与前端（按所选策略动态渲染表单）使用；**不触发第三方导入**。
# 元素字段：
#   key         配置键（写入 base_cfg["policy"][key]；运行时经 infer config set / POST /v1/infers/config）
#   label       展示名（前端表单标签）
#   type        text | int | float | bool（校验 / 控件类型）
#   required    是否必填（缺失时前端提示；后端在设置时校验非空）
#   runtime     会话内能否运行时修改（True = 会话内改**立即生效**——客户端每次请求现读；
#               False = 握手级配置（只在进入会话 / 重连时随策略指令下发），会话内改需退出重进；
#               status 的 runtime_keys 供前端在会话内禁用这些输入框）

#   default     代码缺省（可选；None 表示无缺省）
#   placeholder / help  前端输入提示（可选）
#   options     固定候选值（可选；前端渲染 select，后端同时校验）
#   group       分组（"endpoint" = 推理端点项：前端分组展示，并据此门控「进入推理」）
#   multiline   长文本项（prompt / system_prompt 等）：前端渲染为多行 textarea（缺省 6 行）
#   rows        multiline 的行数（可选；缺省用前端缺省值）
#
# 端点配置项：openpi / lerobot-remote 都连 TCP 端点（openpi → ws、lerobot-remote → zenoh）→ 排在最前。
# 与策略自身配置项**完全同级**：同一 schema、同一表单、同一 `infer config set` /
# POST /v1/infers/config 通道、同一校验；也是**会话级**配置（runtime=False：进入会话时构造
# 传输层，会话内改需退出重进）；**没有专用命令**（旧 `infer ip` / `infer port` 已移除）。
POLICY_ENDPOINT_CONFIG_ITEMS: list[dict] = [
    {
        "key": "host",
        "label": "推理节点地址 host",
        "type": "text",
        "required": False,  # 后端不硬性要求：前端按 group=endpoint 的项是否填齐门控「进入推理」
        "runtime": False,  # 会话级：进入会话时构造传输层，会话内改需退出重进
        "group": "endpoint",
        "default": None,
        "placeholder": "如 127.0.0.1",
        "help": "推理节点地址（openpi 也接受完整 `ws://host:port`）；**进入会话时生效，会话内改需退出重进**",
    },
    {
        "key": "port",
        "label": "推理节点端口 port",
        "type": "int",
        "required": False,
        "runtime": False,
        "min": 1,
        "max": 65535,
        "group": "endpoint",
        "default": None,
        "placeholder": "如 8080",
        "help": "推理节点端口；**进入会话时生效，会话内改需退出重进**",
    },
    {
        "key": "endpoint",
        "label": "远程推理端点 endpoint",
        "type": "text",
        "required": False,
        "runtime": False,
        "group": "endpoint",
        "default": None,
        "placeholder": "tcp/127.0.0.1:7447",
        "help": "lerobot-remote（zenoh）的端点：`tcp/host:port`；填了就以它为准（否则由 host/port 拼），"
        "`tls/...` 需配 zenoh_config_path；**进入会话时生效，会话内改需退出重进**",
    },
]

# 公共配置项（**所有策略共有**）：预热门控 warmup_required + 模型动作布局 action_layout —— 与策略自
# 身配置项**完全同级**：同一 schema、同一表单、同一 `infer config set` / POST /v1/infers/config
# 通道、同一校验；也是**会话级**配置（runtime=False：进入会话时读取，会话内改需退出重进）。
POLICY_COMMON_CONFIG_ITEMS: list[dict] = [
    {
        "key": "warmup_required",
        "label": "必须先预热 warmup_required",
        "type": "bool",
        "required": False,
        "runtime": False,  # 会话级：会话内改需退出重进（已进入的会话不受影响）
        "default": True,
        "help": "true（缺省）= 未预热（`infer connect`：连接 + prepare + 取一块丢弃，**不下发任何动作**）"
        "时拒绝 `infer rollout`（409）；false = 允许 rollout 惰性自连（脚本 / 联调用的后门）",
    },
    {
        "key": "action_layout",
        "label": "模型动作布局 action_layout",
        "type": "text",
        "required": False,
        "runtime": False,  # 会话级：进入会话时固化（下发通路固定）
        "default": "joint",
        "options": [
            "joint",
            "joint_delta",
            "joint+gripper",
            "joint_delta+gripper_delta",
            "pose",
            "pose_delta",
            "pose+gripper",
            "pose_delta+gripper_delta",
        ],
        "placeholder": "joint | joint_delta | joint_delta+gripper_delta",
        "help": "策略**输出**的动作语义（布局词表，段名用 + 连接）：joint 为绝对关节目标；"
        "joint_delta 为叠加到机器人当前 target 的关节增量；可与 gripper / gripper_delta 组合。"
        "布局**不按动作长度猜测**，是否支持及每段维数由机器人能力校验；"
        "**会话内改需退出重进**",
    },
]

POLICY_CONFIG_ITEMS: dict[str, list[dict]] = {
    "openpi": [
        {
            "key": "prompt",
            "label": "文本指令 prompt",
            "type": "text",
            "required": True,
            "runtime": True,
            "multiline": True,
            "default": None,
            "placeholder": "如：把零件放好",
            "help": "语言条件策略：推理 / 录制前必须非空（可经 infer config set 运行时改）",
        },
        {
            "key": "image_size",
            "label": "图像尺寸 image_size",
            "type": "int",
            "required": False,
            "runtime": True,
            "default": 224,
            "placeholder": "224",
            "help": "**端侧上传前的 letterbox 压缩目标**（默认 224，省带宽 / 省端侧算力）：模型端"
            "按 checkpoint 输入尺寸兜底 resize（不匹配时再 letterbox 一次，不变形）；两者相等时"
            "那次 resize 是 no-op。"
            "**每次请求现读**，会话内改立即生效",
        },
    ],
    "lerobot-remote": [
        {
            "key": "deployment",
            "label": "服务端部署名 deployment",
            "type": "text",
            "required": True,
            "runtime": False,
            "default": None,
            "placeholder": "如 press_three_buttons",
            "help": "lerobot-policy-server 的 deployment 名（服务端 server.yaml 的 `deployment`），"
            "发现阶段据此选中实例；**会话内改需退出重进**",
        },
        {
            "key": "semantics",
            "label": "动作语义 semantics",
            "type": "text",
            "required": True,
            "runtime": False,
            "default": None,
            "placeholder": "如 piper-joint-abs-7d-v1",
            "help": "端与服务端约定的动作单位/顺序标识，必须与服务端逐字一致（admission 校验）；**会话内改需退出重进**",
        },
        {
            "key": "observation_layout",
            "label": "状态输入布局 observation_layout",
            "type": "text",
            "required": False,
            "runtime": False,
            "default": "qpos",
            "options": ["qpos", "qpos+qpos_err"],
            "placeholder": "qpos | qpos+qpos_err",
            "help": "state 向量布局：qpos = 关节/夹爪实测值（维度须等于服务端声明）；"
            "qpos+qpos_err 追加机器人提供的目标误差。Edge 不计算 qpos_err；**会话内改需退出重进**",
        },
        {
            "key": "mode",
            "label": "执行模式 mode",
            "type": "text",
            "required": False,
            "runtime": False,
            "default": "chunk",
            "options": ["chunk"],
            "help": "执行模式：chunk = 端侧按块执行（块缓存/切分/预取由 edge RTC 负责）；"
            "RTC 模式需要服务端声明对应能力，暂时只开放 chunk；**会话内改需退出重进**",
        },
        {
            "key": "chunk_merge",
            "label": "块拼接 chunk_merge",
            "type": "text",
            "required": False,
            "runtime": False,
            "default": "append",
            "options": ["append", "aligned"],
            "help": "append = 服务端整块返回、端侧按绝对步号自行跳过过期步；aligned = 额外与服务端"
            "协商游标对齐（需服务端支持 chunk_alignment_v2）；edge RTC 自带对齐，默认 append",
        },
        {
            "key": "encoding",
            "label": "图像编码 encoding",
            "type": "text",
            "required": False,
            "runtime": False,
            "default": "raw",
            "options": ["raw", "jpeg"],
            "help": "raw = 无损 RGB（默认，与训练输入一致）；jpeg 省带宽但有压缩损失；**会话内改需退出重进**",
        },
        {
            "key": "jpeg_quality",
            "label": "JPEG 质量 jpeg_quality",
            "type": "int",
            "required": False,
            "runtime": False,
            "default": 90,
            "help": "encoding=jpeg 时的质量（1-100）；**会话内改需退出重进**",
        },
        {
            "key": "expected_artifact",
            "label": "固定模型制品 expected_artifact",
            "type": "text",
            "required": False,
            "runtime": False,
            "default": None,
            "placeholder": "可选：制品指纹",
            "help": "可选。填了就把会话钉在指定模型/处理器制品上（admission 校验指纹）；留空 = 接受当前部署",
        },
    ],
}


def policy_config_items(policy_type: str) -> list[dict]:
    """策略配置项清单 = 端点项（host / port）+ 公共项（warmup_required）+ 该策略自身配置项。

    未知策略类型 → 仅端点项 + 公共项（无策略自身项）。
    """
    return [
        dict(item)
        for item in POLICY_ENDPOINT_CONFIG_ITEMS + POLICY_COMMON_CONFIG_ITEMS + POLICY_CONFIG_ITEMS.get(policy_type, [])
    ]


def policy_config_keys(policy_type: str) -> set[str]:
    """策略可配置键集合（端点项 + 公共项 + 策略自身项；供运行时设置做白名单校验）。"""
    return {item["key"] for item in policy_config_items(policy_type)}


def policy_config_runtime_keys(policy_type: str) -> set[str]:
    """会话内**可运行时修改**的键（``runtime=True``）。"""
    return {item["key"] for item in policy_config_items(policy_type) if item.get("runtime")}


def policy_features(policy_type: str) -> dict:
    """策略特性速查（由配置项清单派生，供前后端快速判断）。

    - ``requires_prompt`` = 声明了必填的 ``prompt`` 项（语言条件策略，如 openpi）；
    - ``requires_model_path`` = 需要端上指定 checkpoint 路径（旧 gRPC 客户端已移除 → 恒 False：
      ``lerobot-remote`` 的模型由服务端 deployment 决定）；
    - ``lerobot`` = lerobot 远端部署类策略（端上要求 ``deployment`` / ``semantics`` 而非模型路径）。
    """
    items = policy_config_items(policy_type)
    keys = {item["key"] for item in items}
    required = {item["key"] for item in items if item.get("required")}
    return {
        "requires_prompt": "prompt" in required,
        "requires_model_path": "pretrained_name_or_path" in required,
        "lerobot": "deployment" in keys,
    }


def validate_policy_type(policy_type: str) -> str:
    """校验策略注册表键并返回规范值。"""
    if policy_type not in POLICY_REGISTRY:
        available = list(POLICY_REGISTRY.keys())
        raise ValueError(f"Can't find policy type '{policy_type}'. Available types are: {available}")
    return policy_type


def get_policy(base_cfg, policy_type=None):
    """工厂：从注册表按需懒加载并实例化策略客户端。

    policy_type: 可选策略类型（注册表键）；缺省用配置 policy.type（默认 openpi），
                 HTTP / 命令可运行时指定（session run infer 携带）。
    配置段：
      policy:
        host: <推理节点默认地址>
        port: <默认端口>
    策略类型、图像参数和 action_horizon 由具体策略客户端或服务端 metadata 提供。
    """
    shared_config = base_cfg.get("policy", {})
    policy_type = validate_policy_type(policy_type or shared_config.get("type", "openpi"))
    policy_config = {**shared_config, "type": policy_type}

    module_path, class_name = POLICY_REGISTRY[policy_type]
    module = importlib.import_module(module_path)  # 此刻才 import，加载该策略及其第三方依赖
    policy_cls = getattr(module, class_name)

    return policy_cls(policy_config=policy_config)


def policy_adapters():
    """列出所有已注册的策略适配器（不触发第三方包导入）。

    返回 [(type, class_name, module_path), ...]，保持注册顺序。
    """
    return [(ptype, cls_name, module_path) for ptype, (module_path, cls_name) in POLICY_REGISTRY.items()]
