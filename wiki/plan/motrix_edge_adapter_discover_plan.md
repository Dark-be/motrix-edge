# Adapter 发现（discover-driven）实施计划

## 摘要

按 [机器人适配器（adapter）](../design/motrix_edge_adapter.md) 落地：Edge 配置不再声明
adapter 身份，改为向固定端口 `POST /v1/discover` 主动发现机器人进程，身份 / 能力 / 连接
参数全部来自进程自描述；adapter 实例由 `DiscoveredRobot` 参数化。

> **范围**：对应实现随 **feat/6**（任务运行时核心）落地；本 MR（chore/2）仅冻结契约文档，
> 相关项标 `[ ]`。`config/edge.yml` 切换为 `discover` 段由 feat/6 完成（本 MR 内仍为 #2 版）。

## TODO

-   [ ] 1. `adapter/http_contract.py`：扩展 discover 响应字段（`id` / `type` / `action_dim` /
       `observation_keys` / `controllers` / `sensors` / `capabilities` / `endpoint` /
       `shm_name`）。
-   [ ] 2. `adapter/base.py`：新增 `DiscoveredRobot` dataclass（含 `image_names` /
       `to_capabilities()`）。
-   [ ] 3. `adapter/__init__.py`：
        - 新增 `discover_adapter(base_cfg) -> DiscoveredRobot | None`；
        - `get_adapter(base_cfg, discovered=None, required_capability=None)` 改为 discover 驱动、
          adapter 实例参数化；
        - 移除配置驱动的 `adapter_catalog` / `find_adapter_spec` / `AdapterSpec`；
        - `adapter_details(base_cfg)` 改为 discover 驱动。
-   [ ] 4. `adapter/test_adapter.py`：`TestRobotAdapter` 由 `discovered` 参数化（身份 / 能力 /
       `endpoint` / `shm_name`；`discovered=None` 回退类常量）。
-   [ ] 5. `scripts/test_robot_sdk.py`：`/v1/discover` 返回完整自描述块。
-   [ ] 6. `config/edge.yml`：新增 `discover: {host, port}` 段（去掉 `adapter` 身份声明）。
-   [ ] 7. `node.py`：`_probe_adapter` 改为 `discover_adapter` + `get_adapter(discovered=...)`。
-   [ ] 8. `server/app.py`：`_default_robot` / `_adapters` 改为 discover 驱动。
-   [ ] 9. 测试：`tests/test_adapter.py`、`tests/test_node.py`（patch discover_adapter/get_adapter）、
       `tests/test_server.py`（BASE_CFG 改 discover 段）。
-   [ ] 10. `uv run ruff check .` + `uv run pytest` 全绿；格式检查通过。

## 完成标准

-   `config/edge.yml` 不含 adapter `id` / `name` / `type`；只有 `discover` 段。
-   `discover_adapter` 向固定端口发 discover；无进程 → `None`（节点持续重试，不进 ERROR）。
-   `get_adapter(discovered=...)` 按 `discovered.type` 实例化 adapter，身份 / 能力来自进程。
-   `TestRobotAdapter` 不再硬编码身份 / 能力（`discovered` 参数化，缺省回退兼容测试）。
-   全量测试保持全绿。
