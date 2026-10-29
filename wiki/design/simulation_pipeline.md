# Simulation pipeline

## 摘要

独立 Python 3.11 仿真进程实现 Edge 的 robot-pipeline HTTP 与共享内存契约，用于控制与预览联调；不实现采集、训练奖励或真机遥操作。

## 目标与边界

场景来自用户指定的 MotrixGen 工作区、session TOML 和 scene JSON，资产不复制。首版支持单机械臂六关节和一个归一化夹爪，两路 RGB 可绑定任意场景相机。通过专用 simulation_piper adapter 如实声明能力；不声明 pose、depth、capture。

## 执行与观测

通过公开 PrimitiveSkill 接收外部目标，MotrixSession.run_task 持续推进物理，不访问 SDK 私有环境，也不另建物理循环。原生 MotrixSim renderer 在同一执行线程中同步模型并输出 RGB；HTTP 线程只读状态快照及写命令队列，不接触原生句柄。渲染回读非阻塞，状态与图像按同一拍快照发布。停止保持当前实际位置，物理持续运行；reset 仅回到启动姿态，重新布置场景需重启服务。

控制目标经过维数、有限性、关节限位及夹爪范围校验；关节按速度限制逐步下发。命令 ID 在进程内去重并核对载荷，回执区分 queued/applied/rejected/failed。applied 表示目标被执行线程采用，不等于已到位。运动采样含进程实例、目标修订号和单调序号。共享内存只由创建者关闭和删除，不覆盖其他进程的段。

## 验证

使用真实三按钮与叠方块场景验证可配置加载，通过 Edge HttpShmAdapter 读取观测及控制目标，检查实际运动、双路非空图像、停止/回零、非法指令拒绝和退出清理。不实现 capture 端点或数据写盘功能。SDK 自身的任务诊断产物保存在独立运行目录。

## 验证结果

真实三按钮与叠方块场景均通过 HTTP 命令、Edge 适配器及共享内存集成测试，包括关节和夹爪实际运动、双相机图像、重复命令去重、非法动作拒绝、回零、停止及退出清理。测试命令和资产前提见 [使用说明](../../simulation-pipeline/README.md#测试)。
