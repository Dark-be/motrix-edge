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

"""routes 共用助手 —— 命令回执 → HTTP 响应体的形状转换。"""


def accepted(data: dict | None = None) -> dict:
    """命令 ``data`` → 端点响应体：``{"status": "accepted", **data}``。

    与既有端点响应形状一致（回执字段直接摊平到顶层，前端无需感知命令通道）；命令失败
    （业务拒绝 / 超时 / 租约无效）已由 ``CommandService.submit`` 抛成 ``ServiceError``，
    由 app 层统一处理器渲染，故此处只处理成功路径。
    """
    return {"status": "accepted", **(data or {})}


__all__ = ["accepted"]
