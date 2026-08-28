# 推理端点配置 + 虚拟推理端点实施计划

## 摘要

基于[推理策略客户端（policy）](../design/motrix_edge_policy.md)与[命令总线（CommandBus）](../design/motrix_edge_command_bus.md)
的落地计划：交付（1）`scripts/test_infer_point.py` 虚拟 openpi 策略服务端（无真实推理，返回随机游走
action chunk，供联调验证传输契约）；（2）Edge 命令 `infer ip / infer ip set / infer port / infer port set`
（前端推理卡片运行时设置推理端 ip / 端口，写入内存态 `policy` 段，下次 `session run infer` 生效）。
功能完整落地后删除本文档并更新本索引。

## TODO

-   [x] 设计：`wiki/design/motrix_edge_policy.md` 补「运行时端点配置」与「虚拟推理端点」两节；
        `motrix_edge_command_bus.md` 命令清单补 `infer ip / infer ip set / infer port / infer port set`
-   [x] `scripts/test_infer_point.py`：虚拟推理端点（`SimInferCore` 有界随机游走 action chunk +
        WebSocket handler：连接先发 metadata（含 `action_horizon`）、每请求回 `{"action": [horizon, dim]}`；
        CLI `--host/--port/--action-dim/--action-horizon/--step/--range/--seed`）
-   [x] `utils/commands.py`：命令常量 `CMD_INFER_IP / CMD_INFER_IP_SET / CMD_INFER_PORT / CMD_INFER_PORT_SET` + 注册表登记（`infer ip set` 位置参数 `ip`，`infer port set` 位置参数 `port`）+ 端点读写辅助函数（`get_policy_endpoint` / `set_policy_endpoint`，校验非法端口）
-   [x] `node.py`：`_dispatch` 路由推理端点配置命令（全局可用，任何状态）→ 写 `base_cfg["policy"]`
        （内存态），回执带回更新后端点
-   [ ] `server/command.py`：capability `infer_ip / infer_port / infer_ip_set / infer_port_set`
        （submit 同步回执，经命令总线与本地行为一致）——随 **feat/3**（http 控制面）落地
-   [ ] `server/infer.py`：`status()` 补 `endpoint` 字段（当前配置的推理端 host / port）——随 **feat/3**（http 控制面）落地
-   [x] `config/edge.yml`：补 `policy` 段默认端点（host / port 注释示例）
-   [ ] 前端：`types.ts` `InferStatus` 补 `endpoint`；`api.ts` 补 `setInferIp / setInferPort`
        （经 `/v1/commands` capability）；`InferPanel.tsx` 推理卡片补端点显示 + 设置输入框 / 按钮——随 **feat/3**（web console）落地
-   [x] 测试：命令解析（`infer ip set 1.2.3.4` 最长前缀 + 位置参数）、节点分发（get/set/非法端口
        rejected）、虚拟端点 wire 契约（真实 `MsgpackTransport`
        连接 → metadata + 动作块连续随机游走）
-   [ ] 测试：HTTP capability（`test_server.py`）——随 **feat/3**（http 控制面）落地
-   [x] 全量 `npm run format` → `uv run ruff check .` → `uv run pytest`

## 后续（不在本期）

-   [ ] 端点配置持久化到 yaml（本期为内存态，重启 edge 后回到配置文件值）
-   [ ] 推理会话中设置端点时提示「下一会话生效」的前端交互
