# 采集元信息选项（capture meta）

## 摘要

采集元信息（采集人员 / 采集任务等）在 `capture.yml` 中维护为**可拓展的「分类 →
选项数组」**（包内默认只读；可写副本位于 `MOTRIX_CONFIG_DIR` 外界目录或数据目录），由
`capture meta` 命令族（list / add / edit / delete / delete-key）创建 /
编辑 / 删除，经 `GET /v1/captures/meta` 暴露给前端作为**选择列表**；选中后仍由现有
`capture sync` 同步到机器人进程（进程保存一轮数据时附加）。

## 目标与原则

-   **单一事实来源**：元信息选项存于 `capture.yml`（`meta` 段；包内默认，外界目录 /
    数据目录可写副本），CLI / HTTP 统一经
    `CaptureMetaStore` 读写（写回保留文件其它顶层键），无内存态副本。
-   **可拓展**：任意分类（key）→ 选项数组；`capture meta add <新key> <值>` 自动创建新分类，
    无需改代码 / 改 schema。
-   **命令化**：`capture meta` 为**配置级命令**（与 `infer ip` 一致）——节点主循环（任何
    状态）与任务态会话循环共用同一处理器，保证「任何状态可用」。
-   **线程安全**：`CaptureMetaStore` 用 `RLock` 保护读写，CLI / HTTP 命令并发管理不互踩。

## capture.yml 结构

```yaml
# 采集元信息选项（capture meta 命令维护；可拓展任意分类 → 选项数组）
meta:
  operator: [张三, 李四]        # 采集人员
  task_name: [桌面前移, 双臂搬运]  # 采集任务
```

`meta` 为顶层映射：分类名 → 选项字符串数组。文件不存在时视为空（命令自动创建）；写回保留
`meta` 之外的其它顶层键。

## 命令形式（决定）

命令词沿用仓库「空格分隔、不用点」约定，注册为 `capture meta <sub>` 多词命令：

| 命令                           | 位置参数           | 语义                                                       |
| ------------------------------ | ------------------ | ---------------------------------------------------------- |
| `capture meta list [key]`      | `key`（可选）      | 列出全部「分类 → 选项」或某分类选项                        |
| `capture meta add <key> <val>` | `key, value`       | 新增选项（分类不存在则创建）；重复 → rejected              |
| `capture meta edit <key> <o> <n>` | `key, old, new` | 编辑选项（`old` → `new` 重命名）；不存在 → rejected        |
| `capture meta delete <key> <v>` | `key, value`      | 删除某分类下选项（分类清空则删除分类）；不存在 → rejected  |
| `capture meta delete-key <key>` | `key`              | 删除整个分类；不存在 → rejected                            |

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

| 方法 | 路径                  | 租约 | 说明                                             |
| ---- | --------------------- | ---- | ------------------------------------------------ |
| GET  | `/v1/captures/meta`   | 无   | 返回 `{meta: {分类: [选项,...]}}`（前端选择列表） |

`capture sync`（`POST /v1/captures/sync` / `capability=capture_sync`）不变：只负责把选中的
元信息（`{operator, task_name, ...}`）同步到机器人进程；选项**管理**经 CLI `capture meta`。

## 分发与状态可用性

-   `CaptureMetaStore`：`utils/capture_meta.py`；缺省可写路径（外界目录 `MOTRIX_CONFIG_DIR` /
    数据目录，首次缺省访问播种包内默认），可注入临时
    路径（测试）。
-   `handle_capture_meta(cmd, store=None)`：`utils/commands.py`；`store` 缺省用默认路径。
-   `EdgeNode._dispatch`：配置级命令，任何状态（INIT/IDLE/READY/ACTIVE/ERROR）先于状态机
    处理器响应（与 `infer ip / infer port` 同一位置）。
-   会话循环（CaptureSession / InferSession）：任务态（ACTIVE）同样响应 `capture meta`
    （经 `BaseSession._on_capture_meta`），保证 ACTIVE 期间命令不因主循环不 poll 而被拒。

## 相关文档

-   命令模型与传输：[命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   采集会话 / `capture sync`：[会话（session）](./motrix_edge_session.md)
-   代码入口：`src/motrix_edge/utils/capture_meta.py`、`src/motrix_edge/utils/commands.py`
