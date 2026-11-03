# Simulation pipeline

独立的 Python 3.11 仿真机器人进程，通过 Edge 的 HTTP 与共享内存契约提供六关节单臂、夹爪和双相机 RGB 控制/预览。**不实现 capture、录制、深度、末端位姿动作或训练奖励**。

## 启动

在本目录执行 `uv sync --python 3.11`，然后使用与 robot-pipeline 同形态的启动脚本：

```bash
bash scripts/start.sh
```

脚本默认启动 `stack_color_blocks`。切换任务时同时覆盖 session 与 scene：

```bash
bash scripts/start.sh \
  --session tasks/press_three_buttons/session.toml \
  --scene assets/scenes/press_three_buttons/scene.json
```

脚本支持 `MOTRIX_SIMULATION_WORKSPACE`、`MOTRIX_SIMULATION_SESSION`、
`MOTRIX_SIMULATION_SCENE`、`MOTRIX_SIMULATION_OUTPUT` 环境变量，额外命令行参数透传给服务。
`--output` 必须是新目录，保存 SDK 任务诊断；脚本未指定输出时自动分配 `/tmp` 新路径。不录制数据集。
默认 HTTP 为 `127.0.0.1:8090`、共享内存为 `simulation_piper_obs`，可通过 `--host`、`--port`、
`--shm-name` 修改。Edge 与服务必须共享 IPC；启动冲突不会删除其他进程的共享内存。

MotrixGen 内部任务最多运行 9000 个控制拍；到达上限、控制任务提前结束或出现运行错误时，pipeline
会清理 renderer 与共享内存并直接退出整个进程。`/v1/discover` 只在仿真线程健康时返回机器人，避免
物理线程已结束但 HTTP 进程仍可发现机器人的半在线状态。需要自动恢复时由 systemd、Docker 或其它
外部进程管理器重启服务。

自定义场景：将 `--session` 与 `--scene` 一起换成同一任务的配置（例如
`tasks/stack_color_blocks/session.toml` 与 `assets/scenes/stack_color_blocks/scene.json`），保留 registry
和资产相对路径。首版限一个六关节机械臂和一个夹爪；任意两路相机通过
`--cameras <头部相机名> <腕部相机名>` 映射为 Edge 的固定相机名。不同机器人拓扑需要新增适配声明。
随机布局先使用任务预览脚本生成 scene.json，再指定它的绝对路径。

`--no-render` 仅用于物理测试，不提供相机观测。正常 Edge 预览使用默认渲染模式，需要可用的图形/Vulkan 环境。

## Edge 接入

在 Edge 根目录执行 `uv sync` 更新适配器注册后，可以发现 `simulation_piper` 适配器。仿真进程和
Edge 包内默认配置都使用 `127.0.0.1:8090`，无需额外配置文件：

```bash
cd /home/pc16/project/motrix-edge
uv run motrix-edge run
```

Edge 进入 `READY` 后，`GET http://127.0.0.1:8000/v1/adapters/current` 返回绑定的
`simulation_piper`。HTTP 与观测读取仍用原链路。相机名为 `cam_head`、`cam_right_wrist`，夹爪为
`[0,1]`，关节为弧度。

```bash
curl -X POST http://127.0.0.1:8090/v1/discover
curl http://127.0.0.1:8090/v1/health
curl -X POST http://127.0.0.1:8090/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{"action":[0.5],"layout":"gripper","arms":["right"],"command_id":"half-open"}'
curl http://127.0.0.1:8090/v1/motion/commands/half-open
curl -X POST http://127.0.0.1:8090/v1/reset
curl -X POST http://127.0.0.1:8090/v1/safe_stop
```

动作布局支持 `joint`、`gripper`、`joint+gripper`。`applied` 表示目标已由控制线程采用，实际到位需要读取运动采样。reset 回到启动后的机械臂姿态，不重置物体；重新布置场景需重启进程。停止清空排队动作并保持当前实际姿态，物理继续运行。

实现与边界见 [设计](../wiki/design/simulation_pipeline.md)。

## 测试

在本目录执行 `uv run --locked pytest`。测试使用真实 SDK 与 GPU 渲染，经 Edge 适配器验证三按钮和叠方块场景。默认查找同级 manipulation-tasks 仓库，可用 `SIMULATION_TEST_WORKSPACE` 指定其他路径；未提供资产时明确跳过。
