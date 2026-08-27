# 命令总线（CommandBus）

## 摘要

Edge 的控制入口统一为**命令总线**（`utils/commands.py`）：命令对象（`Command`）+ 注册式解析
（`CommandRegistry`）+ 同步回执（`CommandBus.submit` / `reply`）。HTTP / 本地 CLI / 本地脚本统一
为命令来源，**本地命令无需租约、不走 HTTP**（进程内总线），HTTP 命令须租约；状态机校验对所有
来源生效。命令名**空格分隔、不用点**，无短别名。

## 目标与原则

-   控制单元 = 命令对象：带参数、带回执、带授权元数据；本地与 HTTP 行为一致（同一执行器、同一回执、同一状态校验）。
-   新增命令 = 注册一个 `CommandSpec`，CLI 与 HTTP 自动获得解析与执行，不改核心循环。
-   本地命令无需租约、不走 HTTP（进程内 `CommandBus`）；HTTP 命令须持有租约。
-   命令名常量、解析、传输、执行全部收敛到 `utils/commands.py` 单点定义。

## 命令对象

```python
@dataclass
class Command:
    name: str                                   # 命令名（空格分隔多词，如 "session run"）
    params: dict = field(default_factory=dict)  # 业务参数
    meta: dict = field(default_factory=dict)    # 授权元数据（lease_id / requester / idempotency_key）
    reply_to: Callable | None = None            # 回执回调（None = fire-and-forget）

@dataclass
class CommandResult:
    status: str = "ok"        # ok / rejected / error
    data: dict = field(default_factory=dict)
    error: str | None = None
    status_code: int = 200
```

## 注册式解析（CommandRegistry）

-   `register(spec)`：注册 `CommandSpec(name, positional)`；命令名用空格分层（如 `session run`）。
-   `parse_argv(argv)`：CLI 文本（`shlex` 分词）→ `Command`——按命令名**最长前缀匹配**（多词），
    剩余按位置参数 / `--key value` / `key=value` 绑定。
-   `get(name)`：命令名 → 规范名；未知抛 `UnknownCommandError`（含已知命令列表）。
-   **没有中心命令词字典**：注册表即唯一来源。

## 传输（CommandBus）

单总线多生产者（web handler + CLI 键盘线程）、单消费者（EdgeNode 主循环）：

-   `push(cmd)`：即发即忘（急停等安全命令）。
-   `submit(cmd, timeout)`：同步等回执（CLI / HTTP）；内部把 `reply_to` 接到结果队列，处理器经
    `cmd.reply_to(result)` 返回；超时抛 `CommandError`（504）。
-   `__call__()`：非阻塞取下一个命令或 `None`（`command_source` 契约，命令源可替换）。

处理器执行完统一 `reply(cmd.request_id, result)`；`EdgeNode._handle` 统一：查注册表 → 校验
`auth`（`meta.lease_id` 存在则走租约校验）→ 调 handler → 回执。

## 命令清单（build_command_registry）

| 命令名                  | 位置参数  | 层级   | 语义                                              | auth |
| ----------------------- | --------- | ------ | ------------------------------------------------- | ---- |
| `session run`           | `session` | 任务级 | 启动会话（选择 + 启动一步完成；capture / infer）  | none |
| `session quit`          | —         | 任务级 | 退出当前会话（→ READY）                           | none |
| `robot reset`           | —         | 机器人 | 复位机器人（仅 adapter 可用）                     | none |
| `robot estop`           | —         | 全局   | 急停（安全停止 + 转 ERROR）                       | none |
| `robot execute`         | `qpos`    | 机器人 | 直接下发 raw 动作（逗号分隔数字，兼容中英文标点） | none |
| `robot teleop`          | `enabled` | 机器人 | 遥操作开关（true/false）                          | none |
| `capture episode start` | —         | 任务级 | 开始一轮采集（adapter.start_capture）             | none |
| `capture episode end`   | —         | 任务级 | 结束一轮采集（adapter.end_capture）               | none |
| `node reset`            | —         | 节点级 | 节点复位 / ERROR 恢复 → IDLE                      | none |
| `infer rollout`         | —         | 任务级 | 单步推理闭环（上传观测 → 下发动作）               | none |
| `infer connect`         | —         | 任务级 | 单次尝试连接推理节点（推理会话内；成功回执含 metadata） | none |
| `infer ip`              | —         | 配置级 | 查询推理节点 IP（内存态 `policy.host`）           | none |
| `infer ip set`          | `ip`      | 配置级 | 设置推理节点 IP（下次 `session run infer` 生效）  | none |
| `infer port`            | —         | 配置级 | 查询推理节点端口（内存态 `policy.port`）          | none |
| `infer port set`        | `port`    | 配置级 | 设置推理节点端口（下次 `session run infer` 生效） | none |
| `capture sync`          | `meta`    | 任务级 | 同步采集元信息（JSON，`--meta`）到机器人进程（采集会话内消费） | none |

可用性：robot / session 命令**仅在 adapter 可用（READY / ACTIVE）时可用**（IDLE / ERROR 下被拒）；
`node reset` 仅 ERROR 下恢复回 IDLE；`robot estop` 与 `infer ip / infer port`（配置级，与节点
状态机解耦）全局可用。CLI 示例：`session run capture`、`robot execute 0,0,0`、`robot teleop true`、
`infer ip set 192.168.1.10`、`infer port set 8765`。

## 本地 vs HTTP（行为对齐）

| 维度     | 本地（CLI / 脚本）                             | HTTP（Console）                |
| -------- | ---------------------------------------------- | ------------------------------ |
| 传输     | 进程内 `CommandBus`（不走 HTTP）               | HTTP → `from_mapping` → submit |
| 租约     | 无需（本地即信任，`meta` 无 `lease_id`）       | 须持有（`meta.lease_id`）      |
| 回执     | `submit` 同步（脚本）/ `push`（键盘）          | `submit` 同步                  |
| 状态校验 | 同一状态机（非法转移同样被拒）                 | 同一状态机                     |
| 语义     | 同一 `CommandSpec` 语义（node / session 实现） | 同一语义                       |

## 相关文档

-   节点命令分发：[节点生命周期（node）](./motrix_edge_node.md)
-   会话命令消费：[会话（session）](./motrix_edge_session.md)
-   HTTP 化落地：[HTTP 控制面（server）](./motrix_edge_server.md)
-   代码入口：`src/motrix_edge/utils/commands.py` —— 随 **feat/6**（任务运行时核心）落地
