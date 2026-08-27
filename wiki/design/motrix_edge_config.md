# 配置与命令行（config / CLI）

## 摘要

`config/` 提供**全局路径常量**（`_GLOBAL_CONFIG.py`，基于包内固定位置推导仓库根，不依赖 CWD）与
**yaml 配置加载**；CLI 入口统一在 `__main__.py`（console script `motrix-edge` 与
`python -m motrix_edge` 共用同一 `main()`）。

## 全局路径常量

| 常量         | 含义                                                                |
| ------------ | ------------------------------------------------------------------- |
| `ROOT_DIR`   | 仓库根目录（本文件向上 4 级推导）                                   |
| `CONFIG_DIR` | 配置目录（`ROOT_DIR/config`）                                       |
| `DATA_PATH`  | 数据目录（采集产物）                                                |
| `LOG_PATH`   | 日志目录（`debug_print` 的 `logs/log_*.txt` 与 `logs/uvicorn.log`） |

## 配置加载

-   运行时加载 `config/<name>.yml`（默认 `edge`），`run --config <path>` 可指定文件路径
    （缺省 `config/edge.yml`）。
-   配置段：

| 段           | 说明                                                                                                                                                         | 消费方         |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------- |
| `INFO_LEVEL` | 日志级别（DEBUG / INFO / ERROR）                                                                                                                             | 日志           |
| `identity`   | 设备身份（edge_id / edge_name / edge_version）                                                                                                               | identity       |
| `lease`      | 租约 ttl / renew_interval                                                                                                                                    | lease          |
| `server`     | HTTP 监听 host / port                                                                                                                                        | server         |
| `adapter`    | 机器人进程发现 host / port（缺省 127.0.0.1:8090）                                                                                                             | node / adapter |
| `capture`    | 采集会话配置（观测由节点级持续写入，`obs_freq` 不再被会话消费）                                                                                            | node / CaptureSession |
| `policy`     | 推理节点默认 host / port；策略类型、图像参数和 action_horizon 由客户端默认值或服务端 metadata 决定   | policy         |
| `upload`     | 本地采集目录与远端上传目标（data_dir / endpoint）                                                                                                            | UploadSession  |

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
