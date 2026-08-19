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
| `DATA_PATH`  | 数据保存目录（采集产物）                                            |
| `LOG_PATH`   | 日志目录（`debug_print` 的 `logs/log_*.txt` 与 `logs/uvicorn.log`） |

## 配置加载

-   运行时加载 `config/<name>.yml`（默认 `edge`），`run --config <path>` 可指定文件路径
    （缺省 `config/edge.yml`）。
-   配置段：

| 段           | 说明                                                                                  | 消费方         |
| ------------ | ------------------------------------------------------------------------------------- | -------------- |
| `INFO_LEVEL` | 日志级别（DEBUG / INFO / ERROR）                                                      | 日志           |
| `identity`   | 设备身份（edge_id / edge_name / edge_version）                                        | identity       |
| `lease`      | 租约 ttl / renew_interval                                                             | lease          |
| `server`     | HTTP 监听 host / port                                                                 | server         |
| `discover`   | 机器人进程发现 host / port（缺省 127.0.0.1:8090）                                     | node / adapter |
| `capture`    | 观测频率 obs_freq（缺省 30 Hz）                                                       | CaptureSession |
| `policy`     | 推理策略（type / host / port / api_key / image_size / image_format / action_horizon） | policy         |

## 命令行接口（CLI）

| 命令                                | 说明                                                                  |
| ----------------------------------- | --------------------------------------------------------------------- |
| `motrix-edge run`                   | 启动节点（阻塞式主循环 + 内嵌 web 线程）；`--config` 指定配置文件路径 |
| `motrix-edge adapters list`         | 列出所有已注册的机器人 / 策略适配器（不触发 SDK 导入）                |
| `motrix-edge adapters detail`       | 列出已注册机器人适配器的能力详情（静态，不探活）                      |
| `motrix-edge version` / `--version` | 显示版本号（单一来源 `pyproject.toml [project].version`）             |
| `motrix-edge --help`                | 查看帮助与可用子命令                                                  |

运行拓扑：`run` = node 主线程持续运行 `EdgeNode`（CLI 键盘线程经注册表解析行命令 → `push` 到
共享 `CommandBus`）+ web 作为独立线程跑 FastAPI（uvicorn 日志写 `logs/uvicorn.log`）。

## 相关文档

-   各包配置细节见对应包文档：[按包索引](./motrix_edge_architecture.md#按包索引分包导航)
-   代码入口：`src/motrix_edge/config/` 与 `src/motrix_edge/__main__.py`（CLI）—— 随 **feat/6**（任务运行时核心）落地
