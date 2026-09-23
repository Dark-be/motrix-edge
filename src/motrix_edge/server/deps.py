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

"""server 依赖容器 —— `create_app` 装配出的服务集合，作为各 router 工厂的唯一入参。

分层（依赖方向单向向下）：

```
routes/*（HTTP 映射）→ deps.Services（本模块）→ controllers（域语义）
                                              ↘ status / state（只读快照）
                                              ↘ CommandBus → node / session（唯一副作用路径）
```

controllers（``meta`` / ``preview`` / ``webrtc``）只依赖 node / bus / store，
**不依赖 FastAPI**；**写路径唯一入口是 ``CommandService``**（REST 端点与 ``/v1/commands``
共用）。FastAPI 只出现在 ``app.py``（装配 + 中间件）与 ``routes/*``（入参 / 出参映射），
故本模块也不依赖它。
"""

from dataclasses import dataclass

from motrix_edge.identity import Identity
from motrix_edge.lease import LeaseManager
from motrix_edge.node import EdgeNode
from motrix_edge.server.command import CommandService
from motrix_edge.server.meta import CaptureMetaService
from motrix_edge.server.preview import PreviewService
from motrix_edge.server.webrtc import WebRTCService
from motrix_edge.session.upload_session import UploadSession


@dataclass
class Services:
    """一次 ``create_app`` 装配出的全部服务（缺省为 None 的项表示未注入 → 对应端点 501）。

    ``identity`` / ``leases`` / ``uploads`` 总是有值（缺省按 base_cfg 自建）；
    其余由调用方（``__main__`` 或测试）注入：``node`` 供只读快照，``commands`` 是
    **唯一写通道**，``meta`` 供采集元信息选项（直连 store），``preview`` / ``webrtc`` 各自对应一个协议面。
    """

    identity: Identity
    leases: LeaseManager
    uploads: UploadSession
    node: EdgeNode | None = None
    commands: CommandService | None = None
    meta: CaptureMetaService | None = None
    preview: PreviewService | None = None
    webrtc: WebRTCService | None = None


__all__ = ["Services"]
