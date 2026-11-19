# Simulation pipeline

## 摘要

独立 Python 3.11 仿真进程实现 Edge 的 robot-pipeline HTTP 与共享内存契约，用于控制与预览联调；不实现采集、训练奖励或真机遥操作。

## 目标与边界

场景来自用户指定的 MotrixGen 工作区、session TOML 和 scene JSON，资产不复制。支持单机械臂六关节、
Piper 世界系 `grasp_site` 位姿和一个归一化夹爪，两路 RGB 可绑定任意场景相机。通过专用
simulation_piper adapter 如实声明能力；不声明 depth、capture。

## 执行与观测

通过公开 PrimitiveSkill 接收外部目标，MotrixSession.run_task 持续推进物理，不访问 SDK 私有环境，也不另建物理循环。原生 MotrixSim renderer 在同一执行线程中同步模型并输出 RGB；HTTP 线程只读状态快照及写命令队列，不接触原生句柄。渲染回读非阻塞，状态与图像按同一拍快照发布。停止保持当前实际位置，物理持续运行；reset 仅回到启动姿态，重新布置场景需重启服务。

MotrixGen 的通用技能执行器以 9000 控制拍为任务上限。该任务结束即表示仿真机器人离线：pipeline
先关闭 renderer、把共享内存运行位清零并删除共享内存，再向自身发送 `SIGTERM`，使 Uvicorn 与整个
进程退出。`discover` 仅在仿真线程健康时返回机器人，否则回 503；不允许出现物理线程已结束但 HTTP
壳仍可发现机器人的半在线状态。需要继续运行时由外部进程管理器重新启动一个新实例。

控制目标经过维数、有限性、关节限位及夹爪范围校验；`pose` 表示场景 `world` 下的
`grasp_site` 绝对位姿。仿真线程启动时，以 MotrixSim 中法兰的实际世界位姿和同一关节状态的 Piper
Modified-DH 正解求出 `T_world_base`；命令入队前先按 `T_base_tcp = T_world_base^-1 · T_world_tcp`
转换到机器人基座系，再使用阻尼最小二乘逆解原子转换为关节目标。逆解以当前关节目标为首选种子、home
为回退种子，并使用仿真模型限位与 Piper 软限位的交集；不可达、越界或不收敛返回 422，且不修改目标。
输入位姿的原点是 MJCF `grasp_site`；缺省 `T_flange_tcp` 为法兰 `+z` 方向 0.13 m，与任务 Piper
MJCF 定义一致，可由启动参数 `--tcp-offset X Y Z` 覆盖。IK 前先将 TCP 目标还原为法兰目标，FK
对外时做相反换算。健康与发现接口按 Edge 既有工具点契约声明 `pose_frame=tcp`、逐臂
`tcp_offset.right=[0,0,0.13]`，另以 `pose_reference_frame=world` 声明绝对位姿的参考坐标系，使调用方
不靠字段维数猜测坐标语义。
`pose+gripper` 先完成整条动作校验和逆解，再一拍提交关节与夹爪目标。
关节按速度限制逐步下发。命令 ID 在进程内去重并核对原始载荷，回执区分 queued/applied/rejected/failed。
applied 表示目标被执行线程采用，不等于已到位。运动采样含进程实例、目标修订号和单调序号。共享内存只由创建者关闭和删除，不覆盖其他进程的段。

## 任务状态

仿真进程通过任务状态提供器把场景内部事实转换为只读 JSON 快照。提供器只在拥有 SDK
`RuntimeData` 的仿真线程中解析实体与读取关节，HTTP 线程仅在锁内复制最新快照，不直接访问原生句柄。
任务由显式启动参数选择；缺省使用 session 所在目录名。未注册提供器的任务仍可正常控制，状态接口返回
`available=false`，不得用机器人动作已应用或策略会话结束冒充任务成功。新增任务通过注册独立提供器扩展，
不得把任务实体查找、阈值或状态机分支堆入通用控制循环。

`press_three_buttons` 提供器从任务 `success.json` 读取冻结判据，按实际按钮弹簧行程判定：下压至少
1.2 mm 并稳定 40 ms 后锁存一次按压，随后回弹到 0.5 mm 内才完成该按钮；同时记录期望顺序、实际
首次按压顺序和顺序是否有效。三枚按钮均完成按压与回弹且顺序正确时 `success=true`。机器人 reset
同时重置任务状态机，但不篡改物理对象；状态快照继续反映 reset 后的实际按钮位移。

`GET /v1/task/status` 返回提供器、任务、进程实例、采样序号与新鲜度，以及通用 `status`、`success`、
`progress[]` 和任务自定义详情。Edge adapter 与同名 Edge API 透明转发这一快照，Console 只依赖通用
进度项渲染，任务详情保持可扩展。仿真进程尚未就绪、已经退出或状态端点不可达时，Edge adapter
将该可选诊断面降级为不可用，不得让 Console 的周期轮询产生异常或影响推理会话。发现结果的
`capabilities.task_status` 表示当前任务是否有提供器。
状态接口是诊断与评测面，不加入策略 observation，避免训练 / 推理输入契约被任务专用字段污染。

`turn_on_light_switch` 提供器从任务 `success.json` 读取冻结判据，定位唯一 `socket` 的 rocker
关节，按目标限位角误差、关节速度和稳定时间判断开关是否完成切换。状态对外给出当前角度、目标角度、
角速度、稳定时长和一个通用进度项；reset 只清空判定器累计时间，不篡改开关关节。

## 批量策略评测

批量评测由独立 `motrix-simulation-eval` 命令编排，不把场景生成或模型评测逻辑放入通用控制循环。
首版是单 worker 串行执行，并复用一个已经启动的 Edge 服务；每个 episode 都使用独立 pipeline 子进程，
从而隔离 MotrixGen 全局状态、renderer、共享内存和 9000 控制拍上限。远程策略服务可常驻，但每轮重新
建立 Edge 推理会话，禁止跨 episode 复用策略历史。

评测器从任务工作区调用 `python -m tasks.<task> sample-scene`，以 seed 生成任务拥有的 scene JSON；
pipeline 只加载该文件，不导入任务私有模块、不自行随机化场景。随后评测器启动 pipeline、等待其健康，
为本轮签发短租约；若 Edge 因上一 pipeline 下线进入 `ERROR`，则发送 `node/reset` 并等待重新发现到
`READY`。READY 只代表节点生命周期就绪，不代表共享内存首帧已经进入 Edge；评测器还必须持租约轮询
`GET /v1/preview`，直到状态向量和策略 `camera_map` 声明的相机全部出现，之后才能进入推理、异步预热和
持续 rollout。任务结果以 `GET /v1/task/status` 的真实物理判定作为唯一成功来源；动作已应用、推理停止
或超时都不能冒充成功。

每轮达到成功、任务失败、超时或基础设施错误后，评测器停止 rollout、退出推理会话、撤销自己的租约并
终止 pipeline。输出目录按 seed 保留场景、随机化摘要、pipeline 日志和 `result.json`；批次根目录维护
`episodes.jsonl` 与 `summary.json`。`--resume` 只跳过已有终态结果的 seed，未完成目录可以重试，但不得
覆盖一个已成功 episode。首版遇到非本评测器持有的有效租约或已有会话时直接失败，不擅自撤销或接管。

## 验证

使用真实 `press_three_buttons`、`stack_color_blocks` 与 `turn_on_light_switch` 场景验证可配置加载，通过 Edge
HttpShmAdapter 读取观测及控制目标，检查实际运动、双路非空图像、停止/回零、非法指令拒绝和退出清理。
每个场景使用同名任务目录的 session 配置；不实现 capture 端点或数据写盘功能。SDK 自身的任务诊断产物保存在独立运行目录。

## 验证结果

真实三按钮、叠方块与单开关场景均通过 HTTP 命令、Edge 适配器及共享内存集成测试，包括关节和夹爪实际运动、双相机图像、任务状态、重复命令去重、非法动作拒绝、回零、停止及退出清理。测试命令和资产前提见 [使用说明](../../simulation-pipeline/README.md#测试)。
