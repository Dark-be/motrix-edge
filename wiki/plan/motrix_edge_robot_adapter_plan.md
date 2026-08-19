# Robot Adapter（硬件抽象层）实施计划

## 摘要

基于 [机器人适配器（adapter）](../design/motrix_edge_adapter.md) 的落地计划：
新增 `adapter/` 包（RobotAdapter ABC + entry point 发现 + TestRobotAdapter），核心
（session / server / **main** / **init**）改为只依赖 adapter；具体机器人（及 controller /
sensor / profile / collector）已移出核心包，由外部包注册接入。功能完整落地后删除本文档并更新本索引。

> **范围说明**：本 MR（脚手架 + 文档）仅交付包骨架与文档；本计划实现项由后续 MR [!5（任务运行时）](/motphys-robotics/motrix-loop/motrix-edge/-/merge_requests/5) 落地，在本 MR 内均为 `[ ]`。

## TODO

-   [ ] 设计：RobotAdapter 契约（discover/health、capabilities、observe、execute、
        capture、rollout、safe_stop + 生命周期辅助）、entry point 发现、观测键单点契约
-   [ ] `adapter/base.py`：`RobotAdapter` ABC + `RobotCapabilities` + `HealthStatus` +
        `KEY_QPOS` / `KEY_ACTION` / `CAMERA_PREFIX` 观测键契约
-   [ ] `adapter/__init__.py`：`get_adapter(base_cfg)` 工厂（entry point 懒加载，兼容回退
        `robot` 段）+ `robot_adapters()` 列表（不触发类加载）
-   [ ] `adapter/test_adapter.py`：`TestRobotAdapter`（无硬件：模拟观测 / action / capture
        随机游走 / rollout），并注册 entry point
-   [ ] `pyproject.toml`：`[project.entry-points."motrix_edge.adapters"]` 注册
        `test_robot` → `TestRobotAdapter`
-   [ ] 重写 `session/capture_session.py`：改用 `get_adapter`，采集沿用 capture session 模型，
        `SessionState` 与回合生命周期不变
-   [ ] 重写 `session/infer_session.py`：改用 `get_adapter`，`obs → infer → rollout` 闭环
-   [ ] `session/base.py`：`safe_stop()` 委托 `self.adapter`
-   [ ] `server/capture.py`：预检读 `env.adapter`（`ready` / `health()`）
-   [ ] `server/app.py` / `__main__.py`：`_adapters()` / `_print_adapters()` 改用
        `motrix_edge.adapter.robot_adapters()`
-   [ ] `collector` / `profile` / `robot` 整包移出核心（外部包实现 `RobotAdapter`，采集 /
        格式 / 路径自含）
-   [ ] `adapter/base.py` capture 下沉：`start_capture(episode_idx)` / `stop_capture(save)` /
        `save_dir` / `committed_episodes`；`TestRobotAdapter` 自含录制（缓冲 → 按自身格式写盘）
-   [ ] `CaptureSession` 只负责把回合信号编排给适配器（采集由 adapter 自含，`observe()` 驱动帧信号）
-   [ ] 移除 resume / suspend：`SIG_ROBOT_SUSPEND` / `SIG_ROBOT_RESUME`、adapter
        `suspend()` / `resume()`、`SessionState.SUSPENDED`、`RunResult.SUSPEND` 相关信号与函数删除
-   [ ] `config/test.yml`：`robot` 段 → `adapter` 段（`type: test_robot` + `save_dir`）
-   [ ] identity 精简：`edge_id` / `edge_name` / `edge_version`（config / `headers()` / 测试同步）
-   [ ] 测试：新增 `test_adapter.py`；`test_collect_env.py` 改用 `TestRobotAdapter`；
        `test_openpi_env.py` monkeypatch `get_adapter`；`test_server.py` 用 adapter 配置
-   [ ] **能力模型**：`AdapterCapability` 枚举（CAPTURE / EXECUTE / STREAMING）；
        `RobotCapabilities.capabilities` 能力 dict + `supports()`；类级 `CAPABILITIES`
-   [ ] `get_adapter(base_cfg, required_capability=None)` 按能力校验；
        `robot_adapters(required_capability=None)` 按能力过滤（缺省懒加载）
-   [ ] `CaptureSession` 要求 `CAPTURE`、`InferSession` 要求 `EXECUTE`
        （`get_adapter(required_capability=...)`，不支持 → ValueError）
-   [ ] `TestRobotAdapter.CAPABILITIES` = CAPTURE + EXECUTE + STREAMING；
        `test_adapter.py` 补能力 dict / supports / 按能力过滤用例
-   [ ] 全量 `uv run ruff check src tests` + `uv run pytest`（含能力模型，全绿）

## 后续（M11/M12，不在本期）

-   [ ] `capabilities` 扩展为结构化能力描述（供 Console capability 校验 / lease 校验）
-   [ ] `discover` 支持硬件枚举（多设备 / 序列号匹配）与详细健康检查（水位 / 时钟 / 相机）
