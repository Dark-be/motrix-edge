# rtc（实时动作块）实施计划

## 目标

把「动作块缓存 / 三元切分 / 时序平滑」从各策略实现中抽离到策略无关的 `rtc/` 子包：策略只实现
`infer_chunk(observation, index)`（返回原始动作块），`RTCManager` 统一负责 `prefix_actions` /
`execution_actions` / `suffix_actions` 切分、重叠加权融合、预取时机与步号推进；参数经
`policy.rtc` 配置 + `infer rtc` 命令 / `POST /v1/infers/rtc` 运行期可查改。

## TODO

-   [x] 设计文档 `wiki/design/motrix_edge_rtc.md`（三元切分 / 平滑 / 命令 / HTTP / 配置）
-   [x] `rtc/` 子包：`base.py`（`ActionChunk` / `ChunkSlice` / 聚合函数表）、`manager.py`
        （`RTCManager`：队列 / 切分 / 融合 / 预取 / `configure` / `status`）、`__init__.py`（`build_rtc`）
-   [x] 策略接口收敛 `BasePolicyClient.infer_chunk(observation, index=None)`：删 `infer` / `drain` 与
        各策略自持缓存
-   [x] openpi：`infer_chunk` = 组装观测 → 请求 → 返回 `ActionChunk`（无游标）；`prepare` 用 `infer_chunk` 预热丢弃
-   [x] act：`infer_chunk` = 按 `index` 发观测 → 取回整块（含 timestep）→ 返回 `ActionChunk(start_index)`
        （删 `_actions` / `_next_timestep` / `_store_action_chunk` / `_cached_remaining` / 聚合）
-   [x] `InferSession`：构造 `self.rtc = build_rtc(policy, policy.rtc)`；单步 / 持续推理改走 `rtc.infer(obs)`；
        `reset` / `session_finish` 同步；`infer rtc` / `infer rtc set` 命令（会话内应用到 live manager）
-   [x] `utils/commands.py`：`CMD_INFER_RTC` / `CMD_INFER_RTC_SET` + `handle_infer_rtc(base_cfg, cmd)`
    -   参数校验（`parse_rtc_params`）
-   [x] `node.py`：`infer rtc` / `infer rtc set` 配置级分发（任何状态可用）
-   [x] `server/infer.py` + `app.py`：status 带 `rtc`；`POST /v1/infers/rtc`（租约 + 回执参数）
-   [x] 前端 `edge-console`：InferPanel 增加 RTC 卡片（状态 + 参数表单）
-   [x] 测试：`tests/test_rtc.py`（切分 / 平滑 / 预取 / disabled / configure）；更新
        `tests/test_policy.py`、`tests/test_act_grpc_client.py`、`tests/test_infer_session.py`、`tests/test_server.py`
-   [x] wiki 同步：`design/index.md`、`architecture.md`、`policy.md`、`session.md`、`server.md`、
        `command_bus.md`、`web_console.md`、`config.md`
-   [x] 校验：容器 `ruff check` / `ruff format` / `pytest`；前端 `tsc` + `build`；本地 prettier md
