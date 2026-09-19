# 采集元信息选项（capture meta）

## 摘要

采集元信息（采集人员 / 采集任务等）在 `capture.yml` 中维护为**可拓展的「分类 →
选项数组」**（包内默认只读；可写副本位于 `MOTRIX_CONFIG_DIR` 外界目录或**状态目录**
（XDG `XDG_STATE_HOME`，不是采集数据目录）），由
管理入口有两条、写的是同一份数据：CLI 命令族 `capture meta`（list / add / edit / delete /
delete-key）与 HTTP `/v1/captures/meta`（GET 读列表，POST / PATCH / DELETE 增改删）。前端（web
console）从 `GET /v1/captures/meta` 取**选择列表**并按返回的分类动态渲染，选项的增 / 改 / 删
也直接走同一族端点（写须租约）；选中后仍由现有 `capture sync` 同步到机器人进程（进程保存
一轮数据时附加）。

## 目标与原则

-   **单一事实来源**：元信息选项存于 `capture.yml`（`meta` 段；包内默认，外界目录 /
    状态目录可写副本），CLI / HTTP 统一经
    `CaptureMetaStore` 读写（写回保留文件其它顶层键），无内存态副本。
-   **可拓展**：任意分类（key）→ 选项数组；`capture meta add <新key> <值>` 自动创建新分类，
    无需改代码 / 改 schema。
-   **命令化**：`capture meta` 为**配置级命令**（与 `infer config` 一致）——节点主循环（任何
    状态）与任务态会话循环共用同一处理器，保证「任何状态可用」。
-   **线程安全**：`CaptureMetaStore` 用 `RLock` 保护读写，且**全进程只有一个实例**
    （见「分发与状态可用性」）——CLI / HTTP / 会话命令共用一把锁，并发管理不互踩。

## capture.yml 结构

```yaml
# 采集元信息选项（capture meta 命令维护；可拓展任意分类 → 选项数组）
meta:
    operator: [张三, 李四] # 采集人员
    task_name: [桌面前移, 双臂搬运] # 采集任务
```

`meta` 为顶层映射：分类名 → 选项字符串数组。文件不存在时视为空（命令自动创建）；写回保留
`meta` 之外的其它顶层键。

## 命令形式（决定）

命令词沿用仓库「空格分隔、不用点」约定，注册为 `capture meta <sub>` 多词命令：

| 命令                              | 位置参数        | 语义                                                      |
| --------------------------------- | --------------- | --------------------------------------------------------- |
| `capture meta list [key]`         | `key`（可选）   | 列出全部「分类 → 选项」或某分类选项                       |
| `capture meta add <key> <val>`    | `key, value`    | 新增选项（分类不存在则创建）；重复 → rejected             |
| `capture meta edit <key> <o> <n>` | `key, old, new` | 编辑选项（`old` → `new` 重命名）；不存在 → rejected       |
| `capture meta delete <key> <v>`   | `key, value`    | 删除某分类下选项（分类清空则删除分类）；不存在 → rejected |
| `capture meta delete-key <key>`   | `key`           | 删除整个分类；不存在 → rejected                           |

示例：

```
capture meta list
capture meta add operator 王五
capture meta edit operator 王五 王五（二期）
capture meta delete operator 王五（二期）
capture meta delete-key operator
```

回执统一 `ok(meta=更新后全量)`（list 时 `ok(meta=...)`）；参数缺失 / 非法 / 不存在 →
`rejected`（`400`，不崩溃）。

## HTTP 暴露

选项的**读取与管理都在 capture 族**（`/v1/captures/meta`），与 CLI `capture meta` 命令族共用
同一个 `CaptureMetaStore`（进程内单实例 / 一把锁）——两条路径读写的是同一份数据：

| 方法   | 路径                            | 租约 | 说明                                                    |
| ------ | ------------------------------- | ---- | ------------------------------------------------------- |
| GET    | `/v1/captures/meta`             | 无   | 返回 `{meta: {分类: [选项,...]}}`（前端选择列表）       |
| POST   | `/v1/captures/meta`             | 必需 | 新增选项 `{key, value}`（分类不存在则创建）；重复 `400` |
| PATCH  | `/v1/captures/meta`             | 必需 | 重命名选项 `{key, old, new}`；不存在 / 重复 `400`       |
| DELETE | `/v1/captures/meta?key=&value=` | 必需 | 删除选项（分类清空则一并删除分类）；不存在 `400`        |
| DELETE | `/v1/captures/meta/{key}`       | 必需 | 删除整个分类；不存在 `400`                              |

写操作回执与 GET **同构**（`{meta: 全量}`），前端写后不需要再拉一次；读**免租约**（只是选项
列表），写**须持租约**（改的是设备上的配置文件）。选项管理是**配置级**操作：与机器人进程 /
会话状态无关（未绑定 adapter、未进采集会话都能维护），也不经 `/v1/commands`（命令映射表里
没有 `capture meta *`——那是机器人 / 会话控制通道）。

`capture sync`（`POST /v1/captures/sync`）不变：只负责把选中的元信息
（`{operator, task_name, ...}`）同步到机器人进程；它是**会话内命令**——只有在采集会话
（ACTIVE）内提交才会成功，READY（无会话）或推理会话下被拒绝（`409 not applicable`）。
（选项管理不受此限：见上表，配置级、任何状态可用。）

**只读降级**：`CaptureMetaStore` 构造不做 IO（不再因配置目录不可写而让节点启动失败）；
包内默认选项在**首次读 / 写时惰性播种**，播种失败只记 WARNING（读得到空集合），写盘失败
→ `500`（明确错误，不让 `OSError` 冒到节点主循环）。

**并发读写**：读与写共用同一把 `RLock`；写盘走**原子替换**（同目录临时文件 + `os.replace`）——
直接 `open(path, "w")` 会让并发的 `GET /v1/captures/meta` 读到半截 YAML（`load_yaml` 报错 → 500），
原子替换后读侧只会看到旧版或新版。写回时**沿用原文件权限**（首次播种落 `0644`）——`mkstemp`
建的是 `0600`，不校正会把「给人改的配置文件」变成仅属主可读写。

## 元信息的唯一载体

采集元信息（采集员 / 任务名 / 以后新增的任意分类）**只有 `meta` 一个载体**：
`capture sync` 同步的是整个 dict，机器人进程把它附加到 episode 描述文件，`/v1/capture/status`
也原样回报 `meta`——**不设** `operator` / `task_name` 之类的同义顶层字段，以免同一事实两处
维护（消费方按需读 `meta["operator"]`）。

## 分发与状态可用性

-   `CaptureMetaStore`：`utils/capture_meta.py`；缺省可写路径（外界目录 `MOTRIX_CONFIG_DIR` /
    状态目录），包内默认在**首次读 / 写时惰性播种**；可注入临时路径（测试）。
-   **进程内单实例**：`EdgeNode` 持有一份，并注入给会话（`get_session(capture_meta_store=…)`）
    与 `CaptureService`（`capture_meta_store or node.capture_meta_store`，最后才自建）——
    全进程**一份数据、一把锁**（路径规则仍由 `CaptureMetaStore` 缺省逻辑统一决定）。
    这里是「唯一持有者 + 依赖注入」，不是模块级全局单例：依赖显式、测试可注入临时
    store、节点构造参数即可替身，也不会出现「多处各自 new 一把锁」。
-   `handle_capture_meta(cmd, store=None)`：`utils/commands.py`；`store` 缺省用默认路径。
-   `EdgeNode._dispatch`：配置级命令，任何状态（INIT/IDLE/READY/ACTIVE/ERROR）先于状态机
    处理器响应（与 `infer config` 同一位置）。
-   会话循环（CaptureSession / InferSession）：任务态（ACTIVE）同样响应 `capture meta`
    （经 `BaseSession._on_capture_meta`），保证 ACTIVE 期间命令不因主循环不 poll 而被拒。

## 未来方向（值来源可插拔）

本清单是**候选值**来源之一，**实际取值**始终由调用方经 `capture sync` 提交（见上节）。
例如 `operator`（采集人员）后续可能直接来自 Console 的**登录凭证**——那时前端把身份直接放进
`meta` 提交即可，本地清单退化为「没有凭证时的兜底选项」；`task_name` 这类现场自由维护的分类
仍适合放本地清单。若将来需要多来源合并（凭证 + 本地 + 远端），在 Store 前加一层 provider，
`GET /v1/captures/meta` 合并返回即可，前端无需感知来源。

## 相关文档

-   命令模型与传输：[命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   采集会话 / `capture sync`：[会话（session）](./motrix_edge_session.md)
-   代码入口：`src/motrix_edge/utils/capture_meta.py`、`src/motrix_edge/utils/commands.py`
