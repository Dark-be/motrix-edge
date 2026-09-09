# 配置与命令行（config / CLI）

## 摘要

`src/motrix_edge/config/` 是**配置子包**：外界配置优先（环境变量 `MOTRIX_CONFIG_DIR`），否则用
包内 **package data** 只读兜底（`edge.yml`）；日志 / 可写状态目录遵循 XDG（`XDG_STATE_HOME`，
缺省回退 CWD）。CLI 入口统一在 `__main__.py`（console script `motrix-edge` 与
`python -m motrix_edge` 共用同一 `main()`）。

> 迁移（issue #10）：仓库根顶层 `config/` 目录与 `_GLOBAL_CONFIG.py`（`ROOT_DIR` / `CONFIG_DIR` /
> `DATA_PATH` / `LOG_PATH` 仓库根推导常量）已删除——顶层 `config/` 与 robot-pipeline 顶层
> `config` 包同名会干扰 ruff isort 的 first-party 分类；`edge.yml` 移入包内作 package data。

## 配置路径与加载

-   **优先级**：① 外界配置目录 `MOTRIX_CONFIG_DIR`（可写，同名 `yml` 覆盖包内默认）；
    ② 包内默认 `src/motrix_edge/config/edge.yml`（`importlib.resources` 只读访问，不可写）。
-   `load_config(name)`：外界文件存在 → 读取；否则若 `name ∈ DEFAULT_CONFIG_FILES`
    （`("edge.yml",)`）读包内默认；均缺失 → `{}`（兜底不抛错）。
-   `run --config <path>`：指定任意 yaml 路径（如 `/etc/motrix-edge/edge.yaml`）；
    **路径不存在 → `SystemExit("error: File ... does not exist.")`**（干净报错，不回显 traceback）。

路径助手（`config/__init__.py`，模块级 `CONFIG_DIR` / `LOG_PATH` 在 import 时按已设环境变量计算）：

| 函数 / 常量                  | 说明                                                             |
| ---------------------------- | ---------------------------------------------------------------- |
| `get_config_dir()`           | 外界配置目录（`MOTRIX_CONFIG_DIR`）；未设置 → `None`（包内默认） |
| `config_path(name)`          | 配置文件真实路径（外界目录存在时）；无外界目录 → `None`          |
| `writable_config_path(name)` | 可写配置路径：外界目录优先，否则落到状态目录（包内默认只读）     |
| `get_log_dir()`              | 日志目录：`XDG_STATE_HOME/motrix`，缺省 `CWD/logs`               |
| `get_state_dir()`            | 可写状态目录：`XDG_STATE_HOME/motrix`，缺省 `CWD`                |
| `CONFIG_DIR` / `LOG_PATH`    | 模块级导出（`LOG_PATH` 供 `debug_print` 与 uvicorn 日志使用）    |

配置段：

| 段           | 说明                                                                                               | 消费方                |
| ------------ | -------------------------------------------------------------------------------------------------- | --------------------- |
| `INFO_LEVEL` | 日志级别（DEBUG / INFO / ERROR）                                                                   | 日志                  |
| `identity`   | 设备身份（edge_id / edge_name / edge_version）                                                     | identity              |
| `lease`      | 租约 ttl / renew_interval                                                                          | lease                 |
| `server`     | HTTP 监听 host / port                                                                              | server                |
| `adapter`    | 机器人进程发现 host / port（缺省 127.0.0.1:8090）                                                  | node / adapter        |
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
