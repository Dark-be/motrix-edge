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

策略客户端 = 通用 msgpack 传输（transport.py）+ 格式契约（contract.py）+ 策略特有行为（openpi/ 等）。
openpi 客户端依赖第三方包（websockets / msgpack），懒加载避免导入 motrix_edge
时因缺依赖报错。
"""

import importlib

# 注册表：策略类型名 -> (模块路径, 类名)
# 懒加载：仅当 get_policy() 选中该类型时才 import 对应模块（连带加载其第三方依赖）。
POLICY_REGISTRY = {
    "openpi": ("motrix_edge.policy.openpi.client", "OpenPIClient"),
    "act": ("motrix_edge.policy.act.client", "ACTClient"),
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
