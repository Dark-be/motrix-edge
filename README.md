# motrix-edge

机器人边缘节点仓库：对现有机器人做统一的二次抽象。同仓承载两层代码，职责分离：

-   **`src/motrix_edge`（edge 包，任务运行时）**——与策略 / 推理服务器（Endpoint）**统一
    交互**：收集 observation、主动请求 action，本地校验后限速**下发控制指令**；同时承载
    任务会话（采集 / 推理）、节点生命周期与 HTTP / WebRTC 控制面。
-   **`robot-pipeline/`（机械臂承载端，独立子项目）**——机械臂**底层控制循环**、数据采集
    （MCAP）与限速逼近；只对 edge 暴露统一的 `/v1` HTTP 契约与共享内存观测，本身不感知
    策略与任务。

## 组成与分工

### edge（`src/motrix_edge`）—— 任务运行时

对上层提供统一入口（CLI / HTTP / WebRTC），对下层经「机器人适配器（RobotAdapter）」接入
机器人进程：

```text
CLI / 控制面 ──CommandBus──▶ EdgeNode（node 生命周期状态机）
                                │  任务会话：Capture / Infer
                                ▼
              InferSession ──ws──▶ 策略服务器（Endpoint，返回受限 action）
                                │
                                ▼
     RobotAdapter（discover + entry point 发现 / 实例化）
            HTTP 指令下行 + 共享内存观测上行
                                │
                                ▼
        robot-pipeline：机械臂底层承载（见下）
```

`motrix_edge` 包内主要模块：

| 模块 / 子包 | 职责                                                                                                                                                |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `node.py`   | `EdgeNode` 生命周期状态机、命令分发、周期任务                                                                                                       |
| `adapter/`  | RobotAdapter HAL：`HttpShmAdapter` 共享基类（HTTP 指令下行 + 共享内存观测上行）+ `DualPiperAdapter` / `TestRobotAdapter`，以及 `/v1` / 共享内存契约 |
| `session/`  | 任务会话：`BaseSession`（基座）+ `CaptureSession` / `InferSession`，`get_session` 工厂；按「基座 + 功能 + 引擎」装配                                |
| `upload.py` | `UploadService`：本地 episode 扫描 / 选择 / 打包与上传队列状态（**不是会话**，服务层直接持有）                                                      |
| `policy/`   | 推理策略客户端（openpi 等）；`InferSession` 内经 ws 请求推理节点                                                                                    |
| `server/`   | HTTP 控制面（`/v1/*`）+ WebRTC 观测推流                                                                                                             |
| `frame/`    | `FrameManager` 观测帧缓冲（preview / WebRTC 消费）                                                                                                  |
| `identity/` | Edge 设备身份声明与请求元数据                                                                                                                       |
| `lease/`    | Edge 级租约机制（`LeaseManager`）                                                                                                                   |
| `utils/`    | 命令总线（CommandBus）/ 采集元信息选项（`capture_meta`）/ data handler 等工具                                                                       |
| `config/`   | 运行目录解析；现场配置优先，包内 `edge.yml` / `capture.yml` 只读兜底                                                                                |

> adapter 经 Python **entry point**（`motrix_edge.adapters` 组）注册接入，当前内置
> `test_robot`（虚拟，离线联调）与 `dual_piper`（双臂 Piper）；外部 SDK / 包亦可按同一
> 机制注册自己的 RobotAdapter，核心无需改动。

### robot-pipeline（`robot-pipeline/`）—— 机械臂底层承载

**只负责机械臂底层**：控制循环、数据采集与限速。它不直接与策略服务器交互，而是作为
edge `adapter` 的真实承载端——robot server 对外提供统一 `/v1` 指令契约与共享内存观测，
edge 经 `RobotAdapter` 下发指令（`execute` / `rollout` / `safe_stop` / `reset` / 采集回合
控制等）并读取观测。

-   **控制循环**：`env`（BaseEnv）30Hz 主循环 + 命令队列；`robot`（BaseRobot）分别维护
    joint/gripper 的 command 与 target，按 `step_rad` 生成关节 command，并用 `sample_state()`
    缓存分离的物理量观测；
-   **数据采集**：`collector`（act_mcap）把每轮 episode 流式写为 Foxglove MCAP + 同名
    JSON 元信息；
-   **遥操作**：Leader→Follower / master→slave 主从跟随；
-   与 motrix_edge 的耦合**仅限契约**：`/v1` 端点、观测键、共享内存布局单点定义在
    `motrix_edge.adapter`（`base.py` / `http_contract.py` / `shm_contract.py`），robot
    server 直接 `import motrix_edge.adapter.*` 复用，不本地拷贝、不依赖 edge 业务逻辑。

> 独立子项目：自带 `pyproject.toml` / `uv.lock` / `.venv`；已在 `pyproject.toml` 声明同仓
> `motrix-edge` 为 path 依赖（editable），`uv sync` 后即可 `import motrix_edge.adapter.*`，
> 无需手工配置 `PYTHONPATH`。详细见
> [robot-pipeline/README.md](robot-pipeline/README.md)。

## 目录结构

```text
src/motrix_edge/     # edge 包（任务运行时）：node / adapter / session / policy /
                     #   server / frame / identity / lease / utils / config
src/motrix_edge/config/edge.yml   # 边缘节点默认配置（package data，只读）
robot-pipeline/      # 机械臂底层承载（独立 uv 子项目）：src 下 config / env /
                     #   robot / server / collector / utils，启动脚本在 scripts/
wiki/                # 设计与计划文档
tests/               # 测试
scripts/             # 联调脚本（虚拟推理端点等）
```

## 快速开始

```bash
uv sync           # 安装核心依赖 + dev 依赖
uv run pytest     # 运行测试（未装可选面时，对应测试文件不收集，见下）
uv run ruff check .
npm run format    # ruff format + prettier
```

### 可选面（`--extra`）

核心依赖之外，**引入额外第三方依赖**的策略客户端 / 服务拆成可选面（`uv sync --extra <名>`）：

| 可选面    | 内容                                       | 引入的依赖                      |
| --------- | ------------------------------------------ | ------------------------------- |
| `openpi`  | openpi 策略客户端（WS + msgpack 一问一答） | `websockets` / `msgpack`        |
| `lerobot` | lerobot-act 策略客户端（gRPC 流式）        | `grpcio` / `torch`（约 690 MB） |
| `webrtc`  | WebRTC 观测推流（浏览器实时预览）          | `aiortc`                        |
| `all`     | 以上全部                                   | —                               |

```bash
uv sync --extra all      # 实机部署 / 联调通常需要全部
uv sync --extra webrtc   # 只补 WebRTC 预览
```

> `torch` 走**阿里云 pytorch-wheels 镜像**（`[tool.uv] find-links`）：官方 CPU 源在国内实测约
> 0.1 MB/s（175.9 MB 要下约 28 分钟），镜像冷启动约 10 秒。**代价**：该镜像页不提供 `#sha256=`，
> uv 无法记录哈希 → `uv.lock` 里 torch 的 wheel **没有 hash**，`uv sync` 不校验镜像下发内容
> （**仅 torch**；其余依赖走内网 `pypi.mp` 索引，带 sha256）。取舍与核对依据见 `pyproject.toml`
> 的 `[tool.uv]` 注释。

不引入额外依赖的客户端 / 服务**不设选项**（如 RPent 面只用核心依赖里的 `httpx2` / `numpy`）。
对应的测试文件在依赖缺失时**不收集**（pytest 头部会列出被跳过的文件），因此不带可选面跑
测试只覆盖核心面。

未装 `lerobot` 时 `policy.type: lerobot-act` 不可用（服务端动作载荷是 `torch.Tensor`，反序列化
需 torch）；未装 `webrtc` 时 `/v1/webrtc/offer` 回 501，其余控制面不受影响。

### 本机变量（`.env`）

复制样例并按本机路径修改：

```bash
cp .env.example .env
```

-   Compose 自动读取项目目录的 `.env`：`docker compose up -d`。
-   裸跑时显式加载：`uv run --env-file .env motrix-edge run`。

运行根分别由 `MOTRIX_EDGE_DIR` 和 `MOTRIX_ROBOT_PIPELINE_DIR` 指定；配置在 `<root>/config`，
日志在 `<root>/logs`。Compose 使用单独的 `*_IN_CONTAINER` 值，避免把宿主绝对路径传入容器。
完整变量见 [`.env.example`](.env.example)。

机器人承载端（robot-pipeline）为独立子项目，需单独 `uv sync`，详见
[robot-pipeline/README.md](robot-pipeline/README.md)。

## 文档

-   架构与设计：[wiki/design/index.md](wiki/design/index.md)；核心总览见
    [边缘节点架构](wiki/design/motrix_edge_architecture.md)
-   实施计划：[wiki/plan/index.md](wiki/plan/index.md)
-   robot-pipeline 子项目：[robot-pipeline/README.md](robot-pipeline/README.md)
-   仓库约定：[CLAUDE.md](CLAUDE.md)

## 开发

提交前依次执行：`npm run format` → `uv run ruff check .` → `uv run pytest`。
