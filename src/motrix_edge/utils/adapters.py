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

"""适配器清单打印（CLI ``motrix-edge adapters list`` / ``adapters detail``）。"""


def print_adapters() -> None:
    """打印所有已注册的机器人 / 策略适配器（motrix-edge adapters list）。"""
    from motrix_edge.adapter import robot_adapters
    from motrix_edge.policy import policy_adapters

    robot_adapters_list = robot_adapters()
    if robot_adapters_list:
        print("Supported robot adapters:")
        for rtype, cls_name, module_path in robot_adapters_list:
            print(f"  {rtype:<30} {cls_name}  ({module_path})")

    policy_adapters_list = policy_adapters()
    if policy_adapters_list:
        print("Supported policy adapters:")
        for ptype, cls_name, module_path in policy_adapters_list:
            print(f"  {ptype:<30} {cls_name}  ({module_path})")

    if not robot_adapters_list and not policy_adapters_list:
        print("No adapters registered.")


def print_adapter_details() -> None:
    """打印所有已注册机器人适配器的能力详情（motrix-edge adapters detail）。

    静态列表（不 discover / 不探活）：``type`` / ``available`` / ``capabilities``；
    id / name 由 discover 赋予，静态列表不列。缺失 SDK / 导入失败跳过。
    """
    from motrix_edge.adapter import adapter_details

    details = adapter_details()
    if not details:
        print("No robot adapters registered.")
        return

    for item in details:
        caps = item.get("capabilities") or {}
        caps_dict = caps.get("capabilities") or {}
        supported = ", ".join(c for c, ok in caps_dict.items() if ok) or "—"
        print(f"{item.get('type', '?'):<24} available={item.get('available')}")
        print(f"    robot_model: {caps.get('robot_model_id')}@{caps.get('robot_model_version')}")
        print(f"    action_dim:  {caps.get('action_dim')}")
        print(f"    cameras:     {', '.join(caps.get('image_names') or []) or '—'}")
        print(f"    capabilities:{supported}")
    print(f"\n{len(details)} robot adapter(s) registered.")


__all__ = ["print_adapter_details", "print_adapters"]
