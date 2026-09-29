# 配置与命令行（config / CLI）

## 摘要

`src/motrix_edge/config/` 是**配置子包**：一个**根目录**同时管配置与日志——
`<根> = $MOTRIX_EDGE_DIR`，未设置时回落 `<cwd>/motrix-edge/`；**配置在 `<根>/config/`、日志在 `<根>/logs/`**。
包内 yml（`edge.yml` / `capture.yml`）**只是示例**（随包分发、只读）：首次访问把示例**播种**到
`<根>/config/`，之后以那份副本为准。CLI 分两处：入口与子命令在 `__main__.py`（console script `motrix-edge` 与
`python -m motrix_edge` 共用同一 `main()`），交互式会话集中在 `utils/cli.py`（`CliSession`）。

> ⚠️ **播种是一次性的**：包内示例的后续更新**不会**自动覆盖现场副本（想回到示例：删掉
> `<根>/config/<name>` 再跑一次）。`<cwd>` 兜底的含义是「从不同目录启动 → 读不同配置 / 写不同日志」，
> 现场请显式设 `MOTRIX_EDGE_DIR`（**宿主**路径，写进 `.env` 给裸跑用）；容器内由
> `docker-compose.yml` 翻译成容器可见路径（缺省 `/root/workspace/motrix-edge`，就在挂载卷内 →
> 持久），**绝不透传宿主路径**——否则会落进容器可写层，宿主机看不到、容器重建即丢。
> robot-pipeline 同构：根 = `$MOTRIX_ROBOT_PIPELINE_DIR` 或 `<cwd>/motrix-robot-pipeline`。

> 迁移：`XDG_STATE_HOME`、`MOTRIX_EDGE_CONFIG_DIR` 已移除——改为「一个根目录变量 + 示例播种」
> （少一个变量、少一层优先级，外与环境变量语义分成 config / logs 两个子目录）；旧行
> `MOTRIX_EDGE_DIR` 曾被 docker compose 用作**仓库挂载目录**，现已改名为 `MOTRIX_EDGE_WORKSPACE_DIR`；
> 容器内的应用根改用 `*_IN_CONTAINER`（缺省值落在挂载卷内），compose 不再把宿主路径转发进容器。

> 迁移（issue #10）：仓库根顶层 `config/` 目录与 `_GLOBAL_CONFIG.py`（`ROOT_DIR` / `CONFIG_DIR` /
> `DATA_PATH` / `LOG_PATH` 仓库根推导常量）已删除——顶层 `config/` 与 robot-pipeline 顶层
> `config` 包同名会干扰 ruff isort 的 first-party 分类；`edge.yml` / `capture.yml` 移入包内作
> package data。

## 环境变量

产品变量统一 `MOTRIX_` 前缀：同一台机器上与其他 Motrix 产品共存时互不干扰。变量名单点定义在
代码里（`config/__init__.py` 的 `ENV_*`、`utils/data_handler.py` 的 `ENV_LOG_*`），避免字面量散落漂移。

| 变量                                     | 消费者         | 说明                                                                                                        |
| ---------------------------------------- | -------------- | ----------------------------------------------------------------------------------------------------------- |
| `MOTRIX_EDGE_DIR`                        | `config`       | **根目录**（配置 `<根>/config` + 日志 `<根>/logs`）；未设 → `<cwd>/motrix-edge/`（`.env` 里写**宿主**路径） |
| `MOTRIX_ROBOT_PIPELINE_DIR`              | robot-pipeline | 同上（另一个项目）；未设 → `<cwd>/motrix-robot-pipeline/`（`.env` 里写**宿主**路径）                        |
| `MOTRIX_EDGE_LOG_FILE`                   | `utils`        | 文件日志开关（`1` / `true` 开启；**缺省关闭**）；两个项目各自落盘，见下方说明表                             |
| `MOTRIX_EDGE_WORKSPACE_DIR`              | docker compose | 挂到容器 `/root/workspace` 的宿主目录（缺省 = 仓库根；见仓库根 `.env.example`）                             |
| `MOTRIX_EDGE_DIR_IN_CONTAINER`           | docker compose | 容器内 edge 根目录（缺省 `/root/workspace/motrix-edge` = 宿主仓库同名子目录，在挂载卷内 → 持久）            |
| `MOTRIX_ROBOT_PIPELINE_DIR_IN_CONTAINER` | docker compose | 同上（robot-pipeline）：缺省 `/root/workspace/robot-pipeline/motrix-robot-pipeline`                         |

> 日志级别**只在配置文件里**（`edge.yml` / robot 侧 `<robot>.yml` 的 `INFO_LEVEL`）——**刻意不设
> 环境变量**：多一个开关就会有人用、就会静默盖掉 yml（容器里转发一次就再也改不动，最难查）。
> 解析**只发生一次**（启动时经 `set_log_level()`，不写 → 代码缺省 `INFO`），且**不写
> `os.environ`**：配置不经进程环境传递，避免继承给子进程或被其他库读到；`debug_print` 只读进程内的
> `_LOG_LEVEL`。

日志落点（两个项目共用同一套变量名，但各自落盘、互不干扰）：

| 项目             | 根目录（配置 `<根>/config` + 日志 `<根>/logs`）                     |
| ---------------- | ------------------------------------------------------------------- |
| `motrix_edge`    | `$MOTRIX_EDGE_DIR`；未设 → `<cwd>/motrix-edge/`                     |
| `robot-pipeline` | `$MOTRIX_ROBOT_PIPELINE_DIR`；未设 → `<cwd>/motrix-robot-pipeline/` |

> 两个项目的目录名都带 `motrix-` 前缀而不是直接用 `robot-pipeline`：否则未设环境变量且从仓库根
> 启动时，配置 / 日志会写进仓库里的 `robot-pipeline/` 源码目录。

> `MOTRIX_EDGE_LOG_FILE` 是**两个项目共用**的开关（同时决定两边是否写文件，但落点各自独立，
> 见上表）；日志级别则各自读自己的 yml（见上）。生效时机：**级别在启动时解析一次（改后需重启）**，
> 而 `MOTRIX_EDGE_LOG_FILE` 每次判定时读取（改后立即对后续调用生效）。
>
> 级别只作用于 `debug_print`（终端 + 文件）：**uvicorn 的级别独立**（两侧都固定
> `log_level="info"`），所以调到 `DEBUG` 不会让 uvicorn 更啰嗦，uvicorn 也不会输出 DEBUG。
>
> uvicorn 的 access 日志行为两侧一致（各自实现 `utils/logging.uvicorn_log_config`）：缺省
> **静默**（NullHandler，防长期运行刷屏）；`MOTRIX_EDGE_LOG_FILE=1` 时只写各自日志根下的
> `logs/uvicorn.log`（RotatingFileHandler 10MB × 5，纯文本）。uvicorn 的启动 / 错误日志写终端；
> edge 侧在终端已打启动卡片时把**启动 INFO** 降到 WARNING（服务地址已由卡片给出，
> `quiet_startup`），WARNING 以上（端口被占用等）照常输出。
> ⚠️ `robot-pipeline` 只有 `python src/server/robot_server.py`（走 `serve()`）会应用该配置。

## 配置路径与加载

-   **优先级**：只有一条——实际配置 = `<根>/config/<name>`（`<根> = $MOTRIX_EDGE_DIR` 或
    `<cwd>/motrix-edge`）；文件不存在时由 :func:`seed_config` 把**包内示例**（`edge.yml` /
    `capture.yml`）播种过去（写入 0644）；包内没有该示例且本地也没有 → `{}`（兜底不抛错）。
-   **`capture.yml`**（采集元信息选项，`capture meta` 命令族维护）：与 `edge.yml` 同一套路径与播种
    语义，播种后就是可写副本；构造不做 IO，首次读 / 写时惰性播种（目录不可写时降级只读）；见
    [采集元信息选项（capture meta）](./motrix_edge_capture_meta.md)。
-   `load_config(name)`：先播种 → 读 `<根>/config/<name>`；读不到 → 回落到包内示例文本；均缺失 → `{}`。
    只读挂载导致播种失败时不阻断（能读就读本地副本）。
-   `run --config <path>`：指定任意 yaml 路径（如 `/etc/motrix-edge/edge.yaml`）；
    **路径不存在 → `SystemExit("error: File ... does not exist.")`**（干净报错，不回显 traceback）。

路径助手（`config/__init__.py`，模块级 `CONFIG_DIR` / `LOG_PATH` 在 import 时按已设环境变量计算）：

| 函数 / 常量                  | 说明                                                                          |
| ---------------------------- | ----------------------------------------------------------------------------- |
| `get_root_dir()`             | 根目录：`$MOTRIX_EDGE_DIR`，缺省 `<cwd>/motrix-edge`                          |
| `get_config_dir()`           | 配置目录：`<根>/config`（总是可写目标）                                       |
| `get_log_dir()`              | 日志目录：`<根>/logs`                                                         |
| `config_path(name)`          | 配置文件实际路径：`<根>/config/<name>`（不一定存在，见 `seed_config`）        |
| `packaged_config_text(name)` | 包内示例 yml 文本（包内没有 → `None`）                                        |
| `seed_config(name)`          | 把包内示例播种到 `<根>/config/<name>`（幂等，已存在不动；写失败抛 `OSError`） |
| `writable_config_path(name)` | 可写配置路径（= `config_path`，配置目录本身就是可写位置）                     |
| `CONFIG_DIR` / `LOG_PATH`    | 模块级导出（`LOG_PATH` 供 `debug_print` 与 uvicorn 日志使用）                 |

-   `CaptureMetaStore` 写 `capture.yml`：直接写 `<根>/config/capture.yml`（首次缺省访问时播种示例）。

-   `CaptureMetaStore` 写 `capture.yml`：用可写配置路径（外界目录优先，否则状态目录；首次缺省
    访问时把包内默认播种到可写位置）。

配置段：

| 段           | 说明                                                                                                                                                                                     | 消费方                       |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| `INFO_LEVEL` | 日志级别（DEBUG / INFO / ERROR）                                                                                                                                                         | 日志                         |
| `identity`   | 设备身份（edge_id / edge_name / edge_version）                                                                                                                                           | identity                     |
| `lease`      | 租约 ttl / renew_interval                                                                                                                                                                | lease                        |
| `server`     | HTTP 监听 host / port                                                                                                                                                                    | server                       |
| `adapter`    | 机器人进程发现 host / port（缺省 127.0.0.1:8090）；启用臂 / 相机为**运行时配置**（命令 / 前端）                                                                                          | node / adapter               |
| `capture`    | 采集会话配置（观测由节点级持续写入，`obs_freq` 不再被会话消费）                                                                                                                          | node / CaptureSession        |
| `policy`     | 推理节点默认 host / port；`policy.rtc` 为实时动作块参数（块长上限 H / 前置段 P / 执行段 E / 后缀段 S / 重叠聚合）；策略专属配置项（prompt / 模型路径等）**运行时给定**（`infer config`） | policy / rtc / policy config |
| `upload`     | 本地采集目录与远端上传目标（data_dir / endpoint）                                                                                                                                        | UploadService                |

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
-   **只在终端启用**：`stdin` 不是 tty（systemd / `nohup` / 管道 / `docker -d`）时不构造
    `PromptSession`、不起输入线程——那些场景没有可读输入（起了只会刷 `^M` 噪音），构造还会打
    `Input is not a terminal (fd=0)` 警告；命令仍可经 HTTP 面下发。
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
