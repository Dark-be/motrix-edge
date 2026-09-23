# 命令总线（CommandBus）

## 摘要

Edge 的控制入口统一为**命令总线**（`command/` 包）：命令对象（`Command`）+ 注册式解析
（`CommandRegistry`）+ 同步回执（`CommandBus.submit` / `reply`）。HTTP / 本地 CLI / 本地脚本统一
为命令来源，**本地命令无需租约、不走 HTTP**（进程内总线），HTTP 命令须租约；状态机校验对所有
来源生效。命令名**空格分隔、不用点**，无短别名。

## 目标与原则

-   控制单元 = 命令对象：带参数、带回执、带授权元数据；本地与 HTTP 行为一致（同一执行器、同一回执、同一状态校验）。
-   新增命令 = 注册一个 `CommandSpec`，CLI 与 HTTP 自动获得解析与执行，不改核心循环。
-   本地命令无需租约、不走 HTTP（进程内 `CommandBus`）；HTTP 命令须持有租约。
-   命令名常量、解析、传输、执行全部收敛到 `command/` 包（`naming` / `core` / `registry` / `params` / `config_commands`），单点定义。

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
    status: str = "ok"        # ok / rejected / error —— **判成败只看它**
    data: dict = field(default_factory=dict)
    error: str | None = None
    code: str | None = None   # edge 错误码（失败必填），见 errors.ErrorCode
```

-   **判成败只看 `status`**（`ok` / `accepted` 为成功）—— HTTP 与 CLI 一致；`code` 不参与判断，
    它是 **edge 错误码**（`motrix_edge.errors.ErrorCode`）。
-   `code` 是**业务层自己的词表**（`conflict` / `lease_expired` / `timeout` …）：HTTP 面把它
    映射成 HTTP 状态码并在响应体回传，CLI 直接展示 —— **状态码是 HTTP 自己的事**，CLI 不遵循
    「200 = OK」。失败回执必须显式给码，漏给按 `internal` 暴露（服务端问题）。

## 注册式解析（CommandRegistry）

-   `register(spec)`：注册 `CommandSpec(name, positional)`；命令名用空格分层（如 `session run`）。
-   `parse_argv(argv)`：CLI 文本（`shlex` 分词）→ `Command`——按命令名**最长前缀匹配**（多词），
    剩余按位置参数 / `--key value` / `key=value` 绑定。
-   `get(name)`：命令名 → 规范名；未知抛 `UnknownCommandError`（含已知命令列表）。
-   **没有中心命令词字典**：注册表即唯一来源。

## 传输（CommandBus）

单总线多生产者（web handler + CLI 键盘线程）、单消费者（EdgeNode 主循环）：

-   `push(cmd)`：即发即忘（急停等安全命令）。
-   **双队列**：安全命令（`CRITICAL_COMMANDS`，现为 `robot estop`）进**旁路队列**，其余进普通队列。
    任务运行期间 node 主循环只 poll 旁路（普通命令由会话消费）——否则一条分钟级长操作
    （如推理预热加载模型）会把急停一起挡在队列里。因此任务运行期间 `robot estop` **不再经过会话**
    （会话内的 estop 分支只在命令源不是总线时命中，属兜底；回执形状与 node 一致：`ok` + `node_state=error`）。
-   `submit(cmd, timeout)`：同步等回执（CLI / HTTP）；内部把 `reply_to` 接到结果队列，处理器经
    `cmd.reply_to(result)` 返回；超时抛 `CommandError`（504）。**超时 ≠ 取消**：命令已被消费、
    处理器会继续执行——故提交时在 `cmd.meta.reply_deadline` 写截止时刻，需要「调用方已放弃就
    不要产生副作用」的处理器（`infer rollout` / `robot execute`：下发动作到真机）在下发前用
    `deadline_exceeded(cmd)` 自查，过期则**丢弃动作**（否则会出现「调用方看到失败、机器人却动了」）；
    迟到回执由内部 sink **丢弃**（不阻塞会话线程）。
-   `__call__()`：非阻塞取下一个命令或 `None`（`command_source` 契约，命令源可替换）。

处理器执行完统一 `reply(cmd.request_id, result)`：消费方（`EdgeNode._handle` / 会话循环）按注册表
分派到 handler，并做**状态机校验**（非法转移一律拒绝，与来源无关）；**租约校验不在消费方**，
而在 HTTP 入口（`CommandService`）——见下节。

## 命令清单（build_command_registry）

| 命令名                    | 位置参数        | 层级   | 语义                                                                        | auth |
| ------------------------- | --------------- | ------ | --------------------------------------------------------------------------- | ---- |
| `session run`             | `session`       | 任务级 | 启动会话（选择 + 启动一步完成；capture / infer）                            | none |
| `session quit`            | —               | 任务级 | 退出当前会话（→ READY）                                                     | none |
| `robot reset`             | —               | 机器人 | 复位机器人（仅 adapter 可用）                                               | none |
| `robot estop`             | —               | 全局   | 急停（安全停止 + 转 ERROR）                                                 | none |
| `robot execute`           | `qpos`          | 机器人 | 直接下发 raw 动作（逗号分隔数字，兼容中英文标点）                           | none |
| `robot teleop`            | `enabled`       | 机器人 | 遥操作开关（true/false）                                                    | none |
| `capture episode start`   | —               | 任务级 | 开始一轮采集（adapter.start_capture）                                       | none |
| `capture episode end`     | —               | 任务级 | 结束一轮采集（adapter.end_capture）                                         | none |
| `node reset`              | —               | 节点级 | 节点复位 / ERROR 恢复 → IDLE                                                | none |
| `infer rollout`           | —               | 任务级 | 单步推理闭环（上传观测 → 下发动作）                                         | none |
| `infer connect`           | —               | 任务级 | 连接 + **启动异步预热**（prepare + 取一块丢弃，不下发动作；立即回执，幂等） | none |
| `capture sync`            | `meta`          | 任务级 | 同步采集元信息（JSON，`--meta`）到机器人进程（采集会话内消费）              | none |
| `capture meta list`       | `key`（可选）   | 配置级 | 列出采集元信息选项（全部分类或某分类）                                      | none |
| `capture meta add`        | `key, value`    | 配置级 | 新增采集元信息选项（分类不存在则创建；重复 → rejected）                     | none |
| `capture meta edit`       | `key, old, new` | 配置级 | 重命名采集元信息选项（不存在 → rejected）                                   | none |
| `capture meta delete`     | `key, value`    | 配置级 | 删除某分类下的采集元信息选项（分类清空则删除分类）                          | none |
| `capture meta delete-key` | `key`           | 配置级 | 删除整个采集元信息分类                                                      | none |

可用性：robot / session 命令**仅在 adapter 可用（READY / ACTIVE）时可用**（IDLE / ERROR 下被拒）；
`node reset` 仅 ERROR 下恢复回 IDLE；`robot estop` 与 `infer config`、`capture meta *`
（配置级，与节点状态机解耦）全局可用——`capture meta` 读写 `capture.yml` 的 `meta` 段，见
[采集元信息选项（capture meta）](./motrix_edge_capture_meta.md)。CLI 示例：`session run capture`、`robot execute 0,0,0`、`robot teleop true`、`infer rollout stop`、
`infer config set '{"host":"10.0.0.9"}'`、`adapter config set '{"enabled_arms": ["right"]}'`、`lease revoke`。

## 回执通道（push / submit）

命令携带 `reply_to` 即 **submit**（调用方同步等回执），缺省为 **push**（即发即忘）：

-   **走 push**：仅 `robot estop` / `node reset` —— 「不能等 / 不该等」的路径：急停不能等回执
    （且可能没有消费方），节点 ERROR 恢复路径也不该阻塞在同步等待上。经 HTTP 调用时回执为
    `accepted`（急停走旁路队列，任务运行期间也即时生效）；
-   **其余全部命令走 submit** —— 「操作要确认成没成」，回执含结果字段（如 teleop 的
    `teleop`/`mode`、episode 的 `episode`/`recording`、execute 的 `action`）。

**入口只有两个**：本地 CLI（进程内 `CommandBus`）与 HTTP 控制面（`CommandService`：REST 端点按
命令词直调、`/v1/commands` 按 capability）。**派发本身单点共用**
（`command/dispatch.py::CommandDispatcher`：注册校验 → push / submit 分流 → 等回执）；
`CommandService` 只在其上加 HTTP 入口要的三件事：租约门、capability 映射、回执 → HTTP 响应。
同一命令在两端**逐条对应** —— 语义、回执、错误类型（`CommandError`）全部相同，差别只在是否
要求租约。HTTP 侧成功回 `data`，业务拒绝 / 超时按 **edge 错误码**抛 `CommandError`（`app.py`
的单一处理器把它映射成 HTTP 状态码 + 响应体 `code`）；`/v1/commands` 则把回执状态
（`ok` / `rejected` / `error`）与 `code` 原样透传为 `CommandResponse.status` / `.code`。

## 本地 vs HTTP（行为对齐）

| 维度     | 本地（CLI / 脚本）                             | HTTP（Console）                                                             |
| -------- | ---------------------------------------------- | --------------------------------------------------------------------------- |
| 传输     | 进程内 `CommandBus`（不走 HTTP）               | HTTP → `CommandService.submit` → `CommandBus`                               |
| 派发     | `CommandDispatcher.dispatch`（单点共用）       | 同（`CommandService` 内部就调它）                                           |
| 租约     | 无需（本地即信任，`meta` 无 `lease_id`）       | 须持有（`meta.lease_id`）                                                   |
| 回执     | `submit` 同步（脚本）/ `push`（键盘）          | `submit` 同步                                                               |
| 错误     | `CommandError`（edge 错误码 `code`）           | 同（`app.py` 映射成 HTTP 状态码 + 回传 `code`）                             |
| 状态校验 | 同一状态机（非法转移同样被拒）                 | 同一状态机                                                                  |
| 语义     | 同一 `CommandSpec` 语义（node / session 实现） | 同一语义                                                                    |
| 来源标记 | `meta.source = cli`                            | `meta.source = http`（`/call` 为 `rpent`）——**仅日志 / 排障，不作授权依据** |

> 除租约外，两端**不应存在任何其它差异**：新增命令或新增校验一律落在 `CommandSpec` 语义或
> 消费方（node / session）状态机里，不得只加在 HTTP 路由或只加在 CLI。

## 相关文档

-   节点命令分发：[节点生命周期（node）](./motrix_edge_node.md)
-   会话命令消费：[会话（session）](./motrix_edge_session.md)
-   HTTP 化落地：[HTTP 控制面（server）](./motrix_edge_server.md)
-   代码入口：`src/motrix_edge/command/` —— 随 **feat/6**（任务运行时核心）落地
