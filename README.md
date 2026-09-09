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
| `session/`  | 任务会话：`CaptureSession`（数采）/ `InferSession`（推理）                                                                                          |
| `policy/`   | 推理策略客户端（openpi 等）；`InferSession` 内经 ws 请求推理节点                                                                                    |
| `server/`   | HTTP 控制面（`/v1/*`）+ WebRTC 观测推流                                                                                                             |
| `frame/`    | `FrameManager` 观测帧缓冲（preview / WebRTC 消费）                                                                                                  |
| `identity/` | Edge 设备身份声明与请求元数据                                                                                                                       |
| `lease/`    | Edge 级租约机制（`LeaseManager`）                                                                                                                   |
| `utils/`    | 命令总线（CommandBus）/ data handler 等工具                                                                                                         |
| `config/`   | 配置加载：`MOTRIX_CONFIG_DIR` 外界目录覆盖 + 包内默认 `edge.yml`（package data，只读兜底）                                                          |

> adapter 经 Python **entry point**（`motrix_edge.adapters` 组）注册接入，当前内置
> `test_robot`（虚拟，离线联调）与 `dual_piper`（双臂 Piper）；外部 SDK / 包亦可按同一
> 机制注册自己的 RobotAdapter，核心无需改动。

### robot-pipeline（`robot-pipeline/`）—— 机械臂底层承载

**只负责机械臂底层**：控制循环、数据采集与限速。它不直接与策略服务器交互，而是作为
edge `adapter` 的真实承载端——robot server 对外提供统一 `/v1` 指令契约与共享内存观测，
edge 经 `RobotAdapter` 下发指令（`execute` / `rollout` / `safe_stop` / `reset` / 采集回合
控制等）并读取观测。

-   **控制循环**：`env`（BaseEnv）30Hz 主循环 + 命令队列；`robot`（BaseRobot）按
    `step_rad` 对 action / target_action **限速逼近**，每帧 `step()` 后回读
    `get_observation()`（qpos + images）；
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
src/motrix_edge/config/edge.yml   # 边缘节点配置（package data；MOTRIX_CONFIG_DIR 同名文件覆盖）
robot-pipeline/      # 机械臂底层承载（独立 uv 子项目）：src 下 config / env /
                     #   robot / server / collector / utils，启动脚本在 scripts/
wiki/                # 设计与计划文档
tests/               # 测试
scripts/             # 联调脚本（虚拟推理端点等）
```

## 快速开始

```bash
uv sync           # 安装依赖（含 dev 依赖）
uv run pytest     # 运行测试
uv run ruff check .
npm run format    # ruff format + prettier
```

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
