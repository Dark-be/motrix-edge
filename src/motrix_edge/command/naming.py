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

"""命令名与对外 capability（命名层）。

``CMD_*`` 是命令名的**单一事实来源**：命令词统一空格分隔（``session run``）；对外
capability 由命令词派生（``robot execute`` → ``robot/execute``），旧扁平拼写保留一版别名
（回执带 ``deprecated=true``）。见 wiki/design/motrix_edge_command_bus.md。
"""

from dataclasses import dataclass

# ===== 命令名（空格分隔，单点定义）===========================================
# 命令词统一**空格分隔、不用点**。分层：
#   session run <type>  启动会话（选择 + 启动一步完成；type = capture / infer，参数）
#   session quit        退出当前会话（无参）
#   robot reset/estop   机器人复位 / 急停（仅 adapter 可用；estop 全局安全命令）
#   node reset          节点复位 / ERROR 恢复 → IDLE
#   infer rollout       单步推理闭环（会话内消费）
#   infer connect       连接 + 启动异步预热（会话内消费；立即回执，可被急停/退出中断）
#   infer rtc           查询 / 设置 RTC 参数（实时动作块管理器；配置级，任何状态可用）
#   infer model         查询 / 设置策略模型路径（lerobot 类策略；运行时提供，配置级）
#   infer config        查询 / 设置策略配置项（每个策略有自己的独立配置项；配置级）
#   capture sync        同步采集元信息到机器人进程（会话内消费）
CMD_SESSION_RUN = "session run"  # 启动会话（参数 session = capture / infer；一步完成）
CMD_SESSION_QUIT = "session quit"  # 退出当前会话
CMD_ROBOT_RESET = "robot reset"  # 复位机器人（仅 adapter 可用时）
CMD_ROBOT_ESTOP = "robot estop"  # 急停（安全停止 + 转 ERROR；全局安全命令）
CMD_ROBOT_EXECUTE = "robot execute"  # 直接下发 raw 动作（位置参数 qpos，逗号分隔数字）
CMD_ROBOT_TELEOP = "robot teleop"  # 设置遥操作（位置参数 enabled = true/false；可选 mode = absolute|delta 人工接管）
CMD_CAPTURE_EPISODE_START = "capture episode start"  # 开始一轮采集（episode 开始）
CMD_CAPTURE_EPISODE_END = "capture episode end"  # 结束一轮采集（episode 结束）
CMD_CAPTURE_SYNC = "capture sync"  # 同步采集元信息（位置参数 meta，JSON；采集会话内消费）
CMD_CAPTURE_META_LIST = "capture meta list"  # 列出采集元信息选项（位置参数 key 可选）
CMD_CAPTURE_META_ADD = "capture meta add"  # 新增采集元信息选项（位置参数 key, value）
CMD_CAPTURE_META_EDIT = "capture meta edit"  # 编辑采集元信息选项（位置参数 key, old, new）
CMD_CAPTURE_META_DELETE = "capture meta delete"  # 删除采集元信息选项（位置参数 key, value）
CMD_CAPTURE_META_DELETE_KEY = "capture meta delete-key"  # 删除采集元信息分类（位置参数 key）
CMD_ADAPTER_CONFIG = "adapter config"  # 查询运行时 adapter 能力配置（enabled_arms / cameras / home）
CMD_ADAPTER_CONFIG_SET = "adapter config set"  # 设置运行时 adapter 能力配置（位置参数 json，JSON 对象）
CMD_ADAPTER_CONFIG_CURRENT = "adapter config current"  # 获取当前绑定 adapter 实际生效的能力配置（启用臂 / 相机）
CMD_LEASE_REVOKE = "lease revoke"  # 撤销 Edge 当前租约（管理员清理幽灵租约，释放可签发槽位）
CMD_NODE_RESET = "node reset"  # 节点复位 / ERROR 恢复 → IDLE
CMD_INFER_ROLLOUT = "infer rollout"  # 推理闭环（无参=单步；continuous=持续；多步/drain 已取消）
CMD_INFER_ROLLOUT_STOP = "infer rollout stop"  # 停止持续推理（回到会话 READY，不退会话也不断策略连接）
CMD_INFER_CONNECT = "infer connect"  # 连接 + 启动异步预热（推理会话内消费；立即回执，重复调用幂等）

CMD_INFER_PROMPT = "infer prompt"  # 设置推理文本指令（位置参数 prompt；会话内运行时可改）
CMD_INFER_RTC = "infer rtc"  # 查询 RTC 参数与运行状态（内存态 policy.rtc）
CMD_INFER_RTC_SET = "infer rtc set"  # 设置 RTC 参数（位置参数 json，JSON 对象；内存态 policy.rtc）
CMD_INFER_MODEL = "infer model"  # 查询 lerobot 类策略的模型路径（内存态 policy.pretrained_name_or_path）
CMD_INFER_MODEL_SET = "infer model set"  # 设置模型路径（位置参数 path；运行时提供，不写回 yaml）
CMD_INFER_CONFIG = "infer config"  # 查询当前策略的配置项（schema 清单 + 当前值 + 缺失必填项）
CMD_INFER_CONFIG_SET = "infer config set"  # 设置策略配置项（位置参数 json，按当前策略 schema 校验）


# ===== 对外 capability（HTTP 面）=============================================
# capability = 命令词的空格 → 斜杠（``robot execute`` → ``robot/execute``）；scope 取命令词
# 首词（robot / capture / infer / node / lease / adapter / session）。命令词因此是**唯一
# 事实来源**，HTTP capability 由它派生，不手写第二张表。见
# wiki/design/motrix_edge_rpent_bridge.md「命名约定」。


def capability_for(command: str) -> str:
    """命令词（``robot execute``）→ 对外 capability（``robot/execute``）。"""
    return " ".join(command.split()).replace(" ", "/")


# 旧 capability 拼写（扁平 snake）→ 规范 capability。**保留一版**以支持调用方渐进迁移
# （前端 / 外部脚本），回执里以 ``deprecated=True`` 提示；到期后整体删除。
LEGACY_CAPABILITIES: dict[str, str] = {
    "estop": capability_for(CMD_ROBOT_ESTOP),
    "reset": capability_for(CMD_NODE_RESET),
    "robot_reset": capability_for(CMD_ROBOT_RESET),
    "robot_execute": capability_for(CMD_ROBOT_EXECUTE),
    "robot_teleop": capability_for(CMD_ROBOT_TELEOP),
    "capture_episode_start": capability_for(CMD_CAPTURE_EPISODE_START),
    "capture_episode_end": capability_for(CMD_CAPTURE_EPISODE_END),
    "capture_sync": capability_for(CMD_CAPTURE_SYNC),
    "infer_connect": capability_for(CMD_INFER_CONNECT),
}


@dataclass(frozen=True)
class CapabilityRef:
    """capability 解析结果：命令词 + 规范 capability + 是否用了旧拼写。"""

    command: str  # 命令词（CMD_* 常量值，空格分隔）
    capability: str  # 规范 capability（斜杠分隔）
    deprecated: bool = False  # 请求用的是旧拼写（回执里提示调用方迁移）


def resolve_capability(raw: str | None) -> CapabilityRef | None:
    """解析 HTTP capability（规范拼写或旧拼写）→ :class:`CapabilityRef`。

    空格 / 斜杠两种写法都接受（``robot execute`` / ``robot/execute``）；空值与 ``None`` → ``None``
    （调用方走骨架分支）；未知拼写不在这里报错——命令词是否被消费由各处理器判定。
    """
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    legacy = LEGACY_CAPABILITIES.get(text)
    if legacy is not None:
        return CapabilityRef(command=legacy.replace("/", " "), capability=legacy, deprecated=True)
    command = text.replace("/", " ")
    return CapabilityRef(command=command, capability=capability_for(command))
