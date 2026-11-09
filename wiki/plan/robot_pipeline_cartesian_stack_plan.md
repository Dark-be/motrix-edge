# robot-pipeline 笛卡尔动作栈实施计划

## 摘要

按 [Piper 笛卡尔运动学](../design/robot_pipeline_cartesian.md) 的约定，将原 MR !38 拆成四层依赖栈：
先落运动学基础，再依次接入 robot 动作布局、Edge 契约与策略推理。每层合并后将下一层 rebase 到
`master`，保持单个 MR 的改动范围可独立审查；整栈合入后删除本计划并更新索引。

## 状态

| 层级 | 范围                                | MR  | 合并后状态 |
| ---- | ----------------------------------- | --- | ---------- |
| 1/4  | Piper FK / Jacobian / IK / 软限位   | !72 | 待合并     |
| 2/4  | robot scoped action layouts         | !69 | 待处理     |
| 3/4  | Edge action / observation contracts | !70 | 待处理     |
| 4/4  | policy inference action layouts     | !71 | 待处理     |

## TODO

-   [ ] MR !72：替代已关闭的 !68，交付 MotrixSim Piper 运动学基础、控制器转换接口、设计文档与离线测试。
-   [ ] MR !69：在 !72 合并后 rebase 到 `master`，交付 robot 侧 action layout、arms 选择和原子目标提交。
-   [ ] MR !70：在 !69 合并后 rebase 到 `master`，交付 Edge action / observation / SHM 契约。
-   [ ] MR !71：在 !70 合并后 rebase 到 `master`，接入 policy inference、失败恢复及最终配置文档。
-   [ ] 整栈验收：四个 MR 依次合并、最终测试通过后，删除本计划并从 `wiki/plan/index.md` 移除入口。
