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

策略客户端 = 传输（``motrix_edge.transport``：openpi → ws+msgpack，act → lerobot gRPC）
            + 格式契约（contract.py）+ 策略特有行为（openpi/、act/）。策略**只负责取推理结果**
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
    "act": ("motrix_edge.policy.act.client", "ACTClient"),
}

# 策略配置项（**静态声明**：每个策略有自己的独立配置项——prompt / 模型路径 / 设备 / 块长…）。
# 供 CLI（`infer config`）与前端（按所选策略动态渲染表单）使用；**不触发第三方导入**。
# 元素字段：
#   key         配置键（写入 base_cfg["policy"][key]；运行时经 infer config set / POST /v1/infers/config）
#   label       展示名（前端表单标签）
#   type        text | int | bool（校验 / 控件类型）
#   required    是否必填（缺失时前端提示；后端在设置时校验非空）
#   runtime     是否可在**会话内**运行时修改（True = 与其它配置项同级，改写后作用于运行中的客户端）
#   locked_when_connected  是否在**策略已连接**后禁改（True 仅用于连接目标：host / port——
#               传输层连接后不能热改，否则与实际连接不一致；未连接时会话内仍可改）
#   default     代码缺省（可选；None 表示无缺省）
#   placeholder / help  前端输入提示（可选）
#   group       分组（"endpoint" = 推理端点：前端归入策略配置表单的公共项）
#
# 公共配置项（所有策略共有）：推理端点——与策略自身配置项**同层级**（同一 schema、同一表单、
# 同一 infer config 通道），唯一差别是连接策略后锁定（locked_when_connected）。
POLICY_COMMON_CONFIG_ITEMS: list[dict] = [
    {
        "key": "host",
        "label": "推理节点 IP host",
        "type": "text",
        "required": False,
        "runtime": True,
        "locked_when_connected": True,
        "group": "endpoint",
        "default": None,
        "placeholder": "如 127.0.0.1",
        "help": "策略连接后锁定（连接目标不能热改）：退出会话或重连前修改；未连接时随时可改",
    },
    {
        "key": "port",
        "label": "推理节点端口 port",
        "type": "int",
        "required": False,
        "runtime": True,
        "locked_when_connected": True,
        "group": "endpoint",
        "default": None,
        "placeholder": "如 8080",
        "help": "策略连接后锁定（连接目标不能热改）：退出会话或重连前修改；未连接时随时可改",
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
            "default": None,
            "placeholder": "如：把零件放好",
            "help": "语言条件策略：推理 / 录制前必须非空（可经 infer prompt 运行时改）",
        },
    ],
    "act": [
        {
            "key": "pretrained_name_or_path",
            "label": "模型路径 pretrained_name_or_path",
            "type": "text",
            "required": True,
            "runtime": True,
            "default": None,
            "placeholder": "/path/to/pretrained_model",
            "help": "lerobot 类策略：运行时提供，服务端据此加载 checkpoint（infer model set）",
        },
        {
            "key": "device",
            "label": "推理设备 device",
            "type": "text",
            "required": False,
            "runtime": True,
            "default": "cpu",
            "help": "下发给服务端的推理设备（GPU 部署填 cuda / cuda:0）",
        },
        {
            "key": "actions_per_chunk",
            "label": "动作块长 actions_per_chunk",
            "type": "int",
            "required": False,
            "runtime": True,
            "default": 50,
            "help": "服务端动作块长 K（与模型 chunk 匹配）",
        },
    ],
}


def policy_config_items(policy_type: str) -> list[dict]:
    """策略配置项清单 = **公共项**（推理端点 host / port）+ 该策略自身配置项（未知类型 → 仅公共项）。"""
    return [dict(item) for item in POLICY_COMMON_CONFIG_ITEMS + POLICY_CONFIG_ITEMS.get(policy_type, [])]


def policy_config_keys(policy_type: str) -> set[str]:
    """策略可配置键集合（公共项 + 策略自身项；供运行时设置做白名单校验）。"""
    return {item["key"] for item in policy_config_items(policy_type)}


def policy_config_runtime_keys(policy_type: str) -> set[str]:
    """会话内**可运行时修改**的键（``runtime=True``）。"""
    return {item["key"] for item in policy_config_items(policy_type) if item.get("runtime")}


def policy_config_connect_locked_keys(policy_type: str) -> set[str]:
    """**策略已连接后禁改**的键（``locked_when_connected=True``，如推理端点 host / port）。"""
    return {item["key"] for item in policy_config_items(policy_type) if item.get("locked_when_connected")}


def policy_features(policy_type: str) -> dict:
    """策略特性速查（由配置项清单派生，供前后端快速判断）。

    ``requires_prompt`` = 声明了必填的 ``prompt`` 项（语言条件策略，如 openpi）；
    ``requires_model_path`` = 声明了必填的 ``pretrained_name_or_path`` 项（lerobot 类，如 act）。
    """
    items = policy_config_items(policy_type)
    required = {item["key"] for item in items if item.get("required")}
    return {
        "requires_prompt": "prompt" in required,
        "requires_model_path": "pretrained_name_or_path" in required,
        "lerobot": "pretrained_name_or_path" in {item["key"] for item in items},
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
