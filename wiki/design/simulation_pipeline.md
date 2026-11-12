# Simulation pipeline

## 摘要

独立 Python 3.11 仿真进程实现 Edge 的 robot-pipeline HTTP 与共享内存契约，用于控制与预览联调；不实现采集、训练奖励或真机遥操作。

## 目标与边界

场景来自用户指定的 MotrixGen 工作区、session TOML 和 scene JSON，资产不复制。支持单机械臂六关节、Piper 基座系 TCP 位姿和一个归一化夹爪，两路 RGB 可绑定任意场景相机。通过专用 simulation_piper adapter 如实声明能力；不声明 depth、capture。

## 执行与观测

通过公开 PrimitiveSkill 接收外部目标，MotrixSession.run_task 持续推进物理，不访问 SDK 私有环境，也不另建物理循环。原生 MotrixSim renderer 在同一执行线程中同步模型并输出 RGB；HTTP 线程只读状态快照及写命令队列，不接触原生句柄。渲染回读非阻塞，状态与图像按同一拍快照发布。停止保持当前实际位置，物理持续运行；reset 仅回到启动姿态，重新布置场景需重启服务。

MotrixGen 的通用技能执行器以 9000 控制拍为任务上限。该任务结束即表示仿真机器人离线：pipeline
先关闭 renderer、把共享内存运行位清零并删除共享内存，再向自身发送 `SIGTERM`，使 Uvicorn 与整个
进程退出。`discover` 仅在仿真线程健康时返回机器人，否则回 503；不允许出现物理线程已结束但 HTTP
壳仍可发现机器人的半在线状态。需要继续运行时由外部进程管理器重新启动一个新实例。

控制目标经过维数、有限性、关节限位及夹爪范围校验；`pose` 使用 Piper Modified-DH
运动学和阻尼最小二乘逆解，在命令入队前原子转换为关节目标。逆解以当前关节目标为首选种子、home
为回退种子，并使用仿真模型限位与 Piper 软限位的交集；不可达、越界或不收敛返回 422，且不修改目标。
输入位姿的原点是 TCP；缺省 `T_flange_tcp` 为法兰 `+z` 方向 0.12 m，与 single/dual Piper 出厂配置一致，
可由启动参数 `--tcp-offset X Y Z` 覆盖。IK 前先将 TCP 目标还原为法兰目标，FK 对外时做相反换算。
`pose+gripper` 先完成整条动作校验和逆解，再一拍提交关节与夹爪目标。
关节按速度限制逐步下发。命令 ID 在进程内去重并核对原始载荷，回执区分 queued/applied/rejected/failed。
applied 表示目标被执行线程采用，不等于已到位。运动采样含进程实例、目标修订号和单调序号。共享内存只由创建者关闭和删除，不覆盖其他进程的段。

## 验证

使用真实 `press_three_buttons` 与 `stack_color_blocks` 场景验证可配置加载，通过 Edge
HttpShmAdapter 读取观测及控制目标，检查实际运动、双路非空图像、停止/回零、非法指令拒绝和退出清理。
每个场景使用同名任务目录的 session 配置；不实现 capture 端点或数据写盘功能。SDK 自身的任务诊断产物保存在独立运行目录。

## 验证结果

真实三按钮与叠方块场景均通过 HTTP 命令、Edge 适配器及共享内存集成测试，包括关节和夹爪实际运动、双相机图像、重复命令去重、非法动作拒绝、回零、停止及退出清理。测试命令和资产前提见 [使用说明](../../simulation-pipeline/README.md#测试)。
