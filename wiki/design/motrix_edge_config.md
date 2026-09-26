# 配置与命令行（config / CLI）

## 摘要

`src/motrix_edge/config/` 是**配置子包**：外界配置优先（环境变量 `MOTRIX_EDGE_CONFIG_DIR`），否则用
包内 **package data** 只读兜底（`edge.yml` / `capture.yml`）；状态 / 日志遵循 XDG（`XDG_STATE_HOME`，
未设时收在 `<cwd>/motrix-edge/`，两个分支结构一致）。CLI 分两处：入口与子命令在 `__main__.py`（console script `motrix-edge` 与
`python -m motrix_edge` 共用同一 `main()`），交互式会话集中在 `utils/cli.py`（`CliSession`）。

> 迁移（issue #10）：仓库根顶层 `config/` 目录与 `_GLOBAL_CONFIG.py`（`ROOT_DIR` / `CONFIG_DIR` /
> `DATA_PATH` / `LOG_PATH` 仓库根推导常量）已删除——顶层 `config/` 与 robot-pipeline 顶层
> `config` 包同名会干扰 ruff isort 的 first-party 分类；`edge.yml` 移入包内作 package data。

## 环境变量

产品变量统一 `MOTRIX_EDGE_` 前缀：同一台机器上与其他 Motrix 产品共存时互不干扰；
`XDG_STATE_HOME` 属 XDG 规范，不加前缀。变量名单点定义在代码里（`config/__init__.py`
的 `ENV_*`、`utils/data_handler.py` 的 `ENV_LOG_*`），避免字面量散落漂移。

| 变量                     | 消费者         | 说明                                                                            |
| ------------------------ | -------------- | ------------------------------------------------------------------------------- |
| `MOTRIX_EDGE_CONFIG_DIR` | `config`       | 外界配置目录（可写；同名 yml 覆盖包内默认）                                     |
| `MOTRIX_EDGE_LOG_FILE`   | `utils`        | 文件日志开关（`1` / `true` 开启；**缺省关闭**）；两个项目各自落盘，见下方说明表 |
| `MOTRIX_EDGE_LOG_LEVEL`  | `utils`        | 日志级别：本变量 > yml 的 `INFO_LEVEL` > `INFO`（启动时解析一次）               |
| `XDG_STATE_HOME`         | `config`       | 状态 / 日志根；各项目落其下子目录（`motrix-edge/`、`motrix-robot-pipeline/`）   |
| `MOTRIX_EDGE_DIR`        | docker compose | 挂到容器 `/root/workspace` 的宿主目录（缺省 = 仓库根；见仓库根 `.env.example`） |

> `INFO_LEVEL` 是 yml 的**配置键**，`MOTRIX_EDGE_LOG_LEVEL` 是**环境变量**：二者刻意不同名——
> 配置文件不掺产品前缀，而环境变量必须与其他 Motrix 产品隔离。级别解析**只发生一次**（启动时经
> `set_log_level()`），且**不写 `os.environ`**：配置不经进程环境传递，避免继承给子进程或被其他库读到；
> `debug_print` 只读进程内的 `_LOG_LEVEL`。

日志落点（两个项目共用同一套变量名，但各自落盘、互不干扰）：

| 项目             | 状态 / 日志目录                                                                                |
| ---------------- | ---------------------------------------------------------------------------------------------- |
| `motrix_edge`    | `$XDG_STATE_HOME/motrix-edge/`（`capture.yml` + `logs/`）；未设 XDG → `<cwd>/motrix-edge/`     |
| `robot-pipeline` | `$XDG_STATE_HOME/motrix-robot-pipeline/`（`logs/`）；未设 XDG → `<cwd>/motrix-robot-pipeline/` |

> `robot-pipeline` 的状态目录名带 `motrix-` 前缀而不是直接用 `robot-pipeline`：否则未设 XDG
> 且从仓库根启动时，日志会写进仓库里的 `robot-pipeline/` 源码目录。

> `MOTRIX_EDGE_LOG_LEVEL` / `MOTRIX_EDGE_LOG_FILE` 都是**两个项目共用**的：前者语义是「一键把
> 两边都调成同一级别」（调试用），要分别设置应改各自的 yml；后者同时决定两边是否写文件，
> 但落点各自独立（见上表）。生效时机不同：**级别在启动时解析一次（改后需重启）**，而
> `MOTRIX_EDGE_LOG_FILE` 每次判定时读取（改后立即对后续调用生效）。
>
> 级别只作用于 `debug_print`（终端 + 文件）：**uvicorn 的级别独立**（两侧都固定
> `log_level="info"`），所以调到 `DEBUG` 不会让 uvicorn 更啰嗦，uvicorn 也不会输出 DEBUG。
> 启动横幅本身也受级别过滤——级别 ≥ `WARNING` 时启动信息静默。
>
> uvicorn 的 access 日志行为两侧一致（各自实现 `utils/logging.uvicorn_log_config`）：缺省
> **静默**（NullHandler，防长期运行刷屏）；`MOTRIX_EDGE_LOG_FILE=1` 时只写各自状态目录下的
> `logs/uvicorn.log`（RotatingFileHandler 10MB × 5，纯文本）；uvicorn 启动 / 错误日志始终写终端。
> ⚠️ `robot-pipeline` 只有 `python src/server/robot_server.py`（走 `serve()`）会应用该配置。

## 配置路径与加载

-   **优先级**：① 外界配置目录 `MOTRIX_EDGE_CONFIG_DIR`（可写，同名 `yml` 覆盖包内默认）；
    ② 包内默认 `src/motrix_edge/config/edge.yml`（`importlib.resources` 只读访问，不可写）。
-   **`capture.yml`**（采集元信息选项，`capture meta` 命令族维护）：与 `edge.yml` 同一套优先级——
    外界目录同名文件可写副本，包内默认只读（写回时落到 `writable_config_path`）；包内默认在
    **首次读 / 写时惰性播种**（构造不做 IO，配置目录不可写时降级只读）；见
    [采集元信息选项（capture meta）](./motrix_edge_capture_meta.md)。
-   `load_config(name)`：外界文件存在 → 读取；否则若 `name ∈ DEFAULT_CONFIG_FILES`
    （`("edge.yml", "capture.yml")`）读包内默认；均缺失 → `{}`（兜底不抛错）。
-   `run --config <path>`：指定任意 yaml 路径（如 `/etc/motrix-edge/edge.yaml`）；
    **路径不存在 → `SystemExit("error: File ... does not exist.")`**（干净报错，不回显 traceback）。

路径助手（`config/__init__.py`，模块级 `CONFIG_DIR` / `LOG_PATH` 在 import 时按已设环境变量计算）：

| 函数 / 常量                  | 说明                                                                        |
| ---------------------------- | --------------------------------------------------------------------------- |
| `get_config_dir()`           | 外界配置目录（`MOTRIX_EDGE_CONFIG_DIR`）；未设置 → `None`（包内默认）       |
| `config_path(name)`          | 配置文件真实路径（外界目录存在时）；无外界目录 → `None`                     |
| `writable_config_path(name)` | 可写配置路径：外界目录优先，否则落到状态目录（包内默认只读）                |
| `get_log_dir()`              | 日志目录：`$XDG_STATE_HOME/motrix-edge/logs`，缺省 `<cwd>/motrix-edge/logs` |
| `get_state_dir()`            | 可写状态目录：`$XDG_STATE_HOME/motrix-edge`，缺省 `<cwd>/motrix-edge`       |
| `CONFIG_DIR` / `LOG_PATH`    | 模块级导出（`LOG_PATH` 供 `debug_print` 与 uvicorn 日志使用）               |

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

-   **单一落点**：`utils/cli.py` 的 `CliSession`（输入循环 + `CommandCompleter` 补全 + `execute_line`
    解析下发 + `format_result` 回执格式化），`__main__.py` 只做入口、子命令与装配。
-   `PromptSession` 提供可编辑行输入、历史记录与命令补全（基于 `CommandRegistry`，CLI / HTTP 共享同一命令契约），
    底部工具栏在识别出命令后提示其位置参数（如 `robot execute` → `参数: qpos`）。
-   `patch_stdout` 使 node / web / 会话线程的 `print` 输出（含 `debug_print`）不打断当前输入行。
-   EOF / Ctrl-C 仅退出 CLI 输入线程；`EdgeNode` 主循环与生命周期清理不受影响。
-   一次性子命令（`adapters` / `version`）直接打印后退出，无需交互会话。
-   行命令提交给共享 `CommandBus`（与 HTTP 同一注册表、同一状态校验与回执语义，**唯一差异是
    租约**）——见 [命令总线（CommandBus）](./motrix_edge_command_bus.md)；`prompt-toolkit` 是
    显式依赖（`pyproject.toml`）。

运行拓扑：`run` = node 主线程持续运行 `EdgeNode`（CLI 键盘线程经注册表解析行命令 → `push` 到
共享 `CommandBus`）+ web 作为独立线程跑 FastAPI。

**文件日志开关**：环境变量 `MOTRIX_EDGE_LOG_FILE=1` 开启文件日志（**缺省关闭**，防长期运行塞满
磁盘）——同时控制 `debug_print` 的 `logs/log_*.txt` 与 uvicorn 的 `logs/uvicorn.log`（access
只写文件）；关闭时只静默 HTTP access（不写文件、不刷终端），uvicorn 启动 / 错误日志仍写终端
（端口占用 bind 失败、uvicorn 内部异常排障可见），终端 `print` 不受影响。

## 相关文档

-   各包配置细节见对应包文档：[按包索引](./motrix_edge_architecture.md#按包索引分包导航)
-   代码入口：`src/motrix_edge/config/`、`src/motrix_edge/__main__.py`（入口 / 子命令）与
    `src/motrix_edge/utils/cli.py`（交互式会话）—— 随 **feat/6**（任务运行时核心）落地
