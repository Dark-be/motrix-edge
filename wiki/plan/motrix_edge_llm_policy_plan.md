# LLM 轨迹策略实施计划

> 设计见 [LLM 轨迹策略（policy/llm）](../design/motrix_edge_llm_policy.md)。
> 本计划只写 what + how；why 归设计文档。

## 范围

Phase 1（本计划）：**模拟闭环**——契约扩展（位姿观测 / 动作空间）+ LLM 轨迹策略客户端 +
模拟机器人笛卡尔执行 + 假 LLM 服务端 + 测试。
Phase 2（真机）：robot-pipeline 真机 IK 与位姿观测、现场联调。

## TODO

### 契约扩展（edge）

-   [x] `adapter/base.py`：新增观测键 `KEY_POSE`、动作空间枚举 `ActionSpace`（`joint` /
        `cartesian_pose`）、每臂位姿维数 `POSE_DIM_PER_ARM = 6`；`RobotAdapter.ACTION_SPACES`
        类常量（缺省仅 `joint`）；`RobotCapabilities` 增 `action_spaces` / `pose_dim`；
        `rollout(action, action_space=None)` 签名增参（缺省 = `joint`，向后兼容）。
-   [x] `adapter/shm_contract.py`：布局增位姿区（offset 紧随 `action` 区）；`pose_dim=0` 时
        版本保持 v2（既有机器人进程零改动），`pose_dim>0` → v3；读者仅在位姿存在时返回
        `observations/pose`。
-   [x] `adapter/http_contract.py`：新增 `FIELD_ACTION_SPACE` + 取值常量 `VALUE_ACTION_SPACE_*`。
-   [x] `adapter/http_shm_adapter.py`：`observe()` 带出位姿；`rollout()` 按动作空间下发
        `action_space` 字段，并对不在 `ACTION_SPACES` 内的请求直接报错。
-   [x] `adapter/test_adapter.py` / `dual_piper_adapter.py`：声明支持的动作空间与位姿维数。

### LLM 策略客户端

-   [x] `policy/llm/trajectory.py`：轨迹点数据类 + JSON 解析（容忍代码块 / 前后文字）+
        校验（维度 / 类型 / 单调性 / 点数 / 越界裁剪）+ **重采样**（位置姿态线性插值、
        夹爪分段常数）→ `[H, dim]` ndarray。
-   [x] `policy/llm/client.py`：`LLMPolicyClient`——`requires_prompt = True`、
        `action_space = cartesian_pose`；观测组装（图像 base64 + 末端位姿 / 夹爪 + prompt + 历史摘要）；
        `bind_adapter` 绑定布局；`prepare` 预热；失败一律返回 `None`；`reset` 清历史。
-   [x] `policy/__init__.py`：注册 `llm` + `POLICY_CONFIG_ITEMS["llm"]` schema（见设计文档表）。
-   [x] `policy/base.py`：`action_space` 类属性（缺省 `joint`）+ `bind_adapter` 增 `arms` 参数。
-   [x] `session/infer_session.py`：`adapter.rollout(action, action_space=...)`（策略声明透传）。

### 模拟侧

-   [x] `scripts/test_robot_sdk.py`：`SimRobotCore` 增**简易运动学**（线性任务空间映射，
        FK/IK 互为逆）+ 位姿写入共享内存；`/v1/rollout` 接受 `action_space=cartesian_pose`
        并做 IK；位姿随 qpos 变化（闭环可验证）；新增 `--no-random-walk`（关闭模拟遥操作
        随机输入，笛卡尔闭环联调用）。
-   [x] `scripts/test_llm_point.py`：假 LLM 服务端（OpenAI 兼容 `POST /v1/chat/completions`），
        按脚本返回轨迹 JSON（朝目标收敛 + 接近后闭合夹爪），供离线端到端联调。

### 测试

-   [x] `tests/test_llm_policy.py`：轨迹解析（含代码块 / 缺字段 / 非法臂名 / 非单调）、
        重采样（插值 / 夹爪分段常数 / 保持 / 限幅）、失败返回 `None`、历史裁剪、prompt 门控。
-   [x] `tests/test_llm_point.py`：模拟端点（观测解析 / 轨迹生成）+ 模拟运动学（FK/IK 互逆 /
        笛卡尔 rollout / 位姿观测）+ **闭环收敛**（轨迹 → 重采样 → 笛卡尔动作 → IK → 限速执行 → 收敛）。
-   [x] `tests/test_shm_contract.py`：v2 兼容（`pose_dim=0`）与 v3 位姿往返。
-   [x] `tests/test_adapter.py`：位姿观测键、支持的动作空间、`pose_dim`。
-   [x] `tests/test_infer_session.py`：动作空间透传（cartesian policy → adapter 收到该值）。

### 文档与收尾

-   [x] `wiki/design/motrix_edge_policy.md` 增 `llm` 行（策略表 / 包结构 / 配置项 / 动作语义 / 布局绑定）。
-   [x] `wiki/design/motrix_edge_adapter.md` 增位姿观测 / 动作空间说明。
-   [x] `wiki/design/index.md`、`wiki/plan/index.md` 登记本设计 / 计划文档。
-   [x] 校验：`ruff format --check` + `ruff check` + `pytest`（容器内 409 passed 1 skipped）+ prettier（md）。

### Phase 2（真机，需现场）

-   [ ] `robot-pipeline/src/robot/base_robot.py`：位姿观测与 `set_target_action_pose()`（IK 钩子）。
-   [ ] `robot-pipeline/src/robot/dual_piper_robot.py`：`get_observation_pose()`（flange pose）与真机 IK。
-   [ ] `robot-pipeline/src/server/contract_server.py`：`/v1/rollout` 接受 `action_space` 并分发。
-   [ ] 现场：手眼标定、欧拉序确认、叠放精度调参。
