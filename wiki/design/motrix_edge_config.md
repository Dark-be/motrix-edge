# 配置与命令行（config / CLI）

## 摘要

`src/motrix_edge/config/` 统一解析配置与日志路径。Edge 与 robot-pipeline 各自只有一个运行根目录，
其下固定使用 `config/` 和 `logs/`；包内 YAML 仅作只读默认值。CLI 分两处：入口与子命令在
`__main__.py`（console script `motrix-edge` 与
`python -m motrix_edge` 共用同一 `main()`），交互式会话集中在 `utils/cli.py`（`CliSession`）。

> 迁移（issue #10）：仓库根顶层 `config/` 目录与 `_GLOBAL_CONFIG.py`（`ROOT_DIR` / `CONFIG_DIR` /
> `DATA_PATH` / `LOG_PATH` 仓库根推导常量）已删除——顶层 `config/` 与 robot-pipeline 顶层
> `config` 包同名会干扰 ruff isort 的 first-party 分类；`edge.yml` 移入包内作 package data。

## 运行目录契约

| 进程           | 根目录变量                  | 未设置时的根目录              |
| -------------- | --------------------------- | ----------------------------- |
| Edge           | `MOTRIX_EDGE_DIR`           | `<cwd>/motrix-edge`           |
| robot-pipeline | `MOTRIX_ROBOT_PIPELINE_DIR` | `<cwd>/motrix-robot-pipeline` |

每个根目录的布局固定：`<root>/config` 放现场配置，`<root>/logs` 放运行日志。路径函数在调用时读取
环境变量，不缓存 import 时的结果；测试、嵌入式调用和同一解释器内重配不会拿到陈旧路径。容器必须传入
**容器内可见路径**，并把对应目录挂载到持久卷；宿主路径不能直接作为容器内路径使用。

`MOTRIX_CONFIG_DIR` 与 `XDG_STATE_HOME` 不再参与这两个进程的路径解析。运行根变量为空时才使用
`<cwd>` 兜底，因此生产与容器部署应显式设置根目录，避免启动目录变化导致读写位置变化。

## 配置路径与加载

-   **优先级**：① `<root>/config/<name>` 中的现场配置；② 包内默认 YAML（只读兜底）。
-   `load_config(name)` 只读，不创建目录或复制文件。现场配置缺失时直接读取包内默认，避免普通启动
    在只读镜像或 bind mount 上产生副作用。
-   **`capture.yml`**（采集元信息选项，`capture meta` 命令族维护）：与 `edge.yml` 同一套优先级——
    现场文件可写、包内默认只读；`CaptureMetaStore` 首次访问时才把默认内容写入
    `<root>/config/capture.yml`（构造不做 IO，目录不可写时降级只读）；见
    [采集元信息选项（capture meta）](./motrix_edge_capture_meta.md)。
-   `load_config(name)`：外界文件存在 → 读取；否则若 `name ∈ DEFAULT_CONFIG_FILES`
    （`("edge.yml", "capture.yml")`）读包内默认；均缺失 → `{}`（兜底不抛错）。
-   `run --config <path>`：指定任意 yaml 路径（如 `/etc/motrix-edge/edge.yaml`）；
    **路径不存在 → `SystemExit("error: File ... does not exist.")`**（干净报错，不回显 traceback）。

路径助手（两个项目各自的 `config/__init__.py` 使用同一语义）：

| 函数 / 常量                  | 说明                                                    |
| ---------------------------- | ------------------------------------------------------- |
| `get_root_dir()`             | 环境变量指定的根；未设置时回落到 `<cwd>/<project-name>` |
| `get_config_dir()`           | `<root>/config`                                         |
| `config_path(name)`          | `<root>/config/<name>`                                  |
| `writable_config_path(name)` | 与 `config_path(name)` 相同，供明确需要写配置的组件使用 |
| `get_log_dir()`              | `<root>/logs`                                           |

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
