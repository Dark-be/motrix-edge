# 配置与命令行（config / CLI）

## 摘要

`config/` 提供**路径解析 + 选择性加载外界配置**（`config/__init__.py`，**环境变量优先、包内
默认兜底**）与 **yaml 配置加载**；CLI 入口统一在 `__main__.py`（console script `motrix-edge`
与 `python -m motrix_edge` 共用同一 `main()`）。

## 路径与配置来源（分层）

> **Edge 不负责数据目录**：采集数据落盘由 adapter / SDK 进程自维护（`data_status` 上报）；
> Edge 只维护**采集配置**（如 `capture.yml` 的操作员 / 任务元信息），经 `capture sync` 传给 adapter。

| 函数 / 常量        | 语义                                                                                                   |
| ------------------ | ------------------------------------------------------------------------------------------------------ |
| `get_config_dir()` | 外界配置目录（环境变量 `MOTRIX_CONFIG_DIR`）；未设置 → None（用包内默认，只读）                        |
| `get_log_dir()`    | 日志目录（`debug_print` 的 `log_*.txt` 与 `uvicorn.log`）：`XDG_STATE_HOME`/motrix，缺省 `CWD/logs`  |
| `get_state_dir()`  | 可写配置状态目录（如 `capture.yml` 元信息）：`XDG_STATE_HOME`/motrix，缺省 `CWD`                    |
| `config_path(name)`  | 配置真实路径：外界目录 → `{config_dir}/{name}`；否则 None（读包内默认）                              |
| `writable_config_path(name)` | 可写配置路径（写操作用）：外界目录优先，否则状态目录（包内默认只读）                     |
| `load_config(name)` | 加载配置：外界文件优先，否则读包内默认 yml（package data，只读兜底）；缺失 → `{}`                     |
| `CONFIG_DIR` / `LOG_PATH` | 模块级便捷常量（import 时计算；`CONFIG_DIR` 无外界配置时为 None）                          |

**包内默认配置**：`src/motrix_edge/config/*.yml`（`edge.yml` / `capture.yml`）作为 package data
打包，经 `importlib.resources` **只读**访问。外界配置只需在 `MOTRIX_CONFIG_DIR` 指向的目录放
同名 yml（可写，覆盖包内默认）。`run --config <path>` 仍可显式指定任意 yaml 路径。

## 配置加载

-   缺省：`load_config("edge.yml")`（`MOTRIX_CONFIG_DIR` 外界配置优先，否则包内默认）；
    `run --config <path>` 显式指定则从该路径加载。
-   `CaptureMetaStore` 写 `capture.yml`：可写路径（外界目录优先，否则状态目录；首次缺省访问时
    把包内默认播种到可写位置）。
-   配置段：

| 段           | 说明                                                                                               | 消费方                |
| ------------ | -------------------------------------------------------------------------------------------------- | --------------------- |
| `INFO_LEVEL` | 日志级别（DEBUG / INFO / ERROR）                                                                   | 日志                  |
| `identity`   | 设备身份（edge_id / edge_name / edge_version）                                                     | identity              |
| `lease`      | 租约 ttl / renew_interval                                                                          | lease                 |
| `server`     | HTTP 监听 host / port                                                                              | server                |
| `adapter`    | 机器人进程发现 host / port（缺省 127.0.0.1:8090）；启用臂 / 相机 / home_qpos 为**运行时配置**（命令 / 前端） | node / adapter        |
| `capture`    | 采集会话配置（观测由节点级持续写入，`obs_freq` 不再被会话消费）                                    | node / CaptureSession |
| `policy`     | 推理节点默认 host / port；策略类型、图像参数和 action_horizon 由客户端默认值或服务端 metadata 决定 | policy                |
| `upload`     | 本地采集目录与远端上传目标（data_dir / endpoint）                                                  | UploadSession         |


## 命令行接口（CLI）

| 命令                                | 说明                                                                  |
| ----------------------------------- | --------------------------------------------------------------------- |
| `motrix-edge run`                   | 启动节点（阻塞式主循环 + 内嵌 web 线程）；`--config` 指定配置文件路径 |
| `motrix-edge adapters list`         | 列出所有已注册的机器人 / 策略适配器（不触发 SDK 导入）                |
| `motrix-edge adapters detail`       | 列出已注册机器人适配器的能力详情（静态，不探活）                      |
| `motrix-edge version` / `--version` | 显示版本号（单一来源 `pyproject.toml [project].version`）             |
| `motrix-edge --help`                | 查看帮助与可用子命令                                                  |

交互式 `run` 使用 `prompt_toolkit` 统一处理终端输入与输出：

-   `PromptSession` 提供可编辑行输入、历史记录与命令补全（基于 `CommandRegistry`，CLI / HTTP 共享同一命令契约），
    底部工具栏在识别出命令后提示其位置参数（如 `robot execute` → `参数: qpos`）。
-   `patch_stdout` 使 node / web / 会话线程的 `print` 输出（含 `debug_print`）不打断当前输入行。
-   EOF / Ctrl-C 仅退出 CLI 输入线程；`EdgeNode` 主循环与生命周期清理不受影响。
-   一次性子命令（`adapters` / `version`）直接打印后退出，无需交互会话。

运行拓扑：`run` = node 主线程持续运行 `EdgeNode`（CLI 键盘线程经注册表解析行命令 → `push` 到
共享 `CommandBus`）+ web 作为独立线程跑 FastAPI（uvicorn 日志写 `logs/uvicorn.log`）。

## 相关文档

-   各包配置细节见对应包文档：[按包索引](./motrix_edge_architecture.md#按包索引分包导航)
-   代码入口：`src/motrix_edge/config/` 与 `src/motrix_edge/__main__.py`（CLI）—— 随 **feat/6**（任务运行时核心）落地
