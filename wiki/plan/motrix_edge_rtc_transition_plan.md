# RTC 过渡策略（权重过渡 / 连续过渡）实施计划

> **状态**：**已落地并在 master**（`aggregate_fn` 含 `continuous`，`status().last_chunk.overlap_steps`
> 上报）；仅剩「实机对比」1 项未勾（见 TODO），故暂不删除本计划。

## 摘要

给 `rtc` 的重叠过渡补齐策略集合：**权重过渡**（固定 `0.3+0.7` 等搭配）与**连续过渡**（按动作步数让
本段权重 1→0、下一段 0→1）。设计见 [实时动作块（rtc）](../design/motrix_edge_rtc.md)。

## 状态

| 项       | 内容                                                                                   |
| -------- | -------------------------------------------------------------------------------------- |
| 范围     | `src/motrix_edge/rtc`、`server`（字段描述）、`config/edge.yml`、前端面板               |
| 契约影响 | 无新增字段；`aggregate_fn` 取值集合新增 `continuous`                                   |
| 验证     | `tests/test_rtc.py`（连续过渡端到端 + 策略表语义），全仓 ruff / prettier / pytest 全绿 |

## TODO

-   [x] 过渡策略表语义升级：表项 = 「下一段的权重曲线」`fn(pos, length) -> alpha`，融合式
        `(1 - alpha) * 本段 + alpha * 下一段`（`rtc/base.py`，默认值 `DEFAULT_AGGREGATE_FN`）。
-   [x] 权重过渡固定搭配：`weighted_average`（0.3 本段 + 0.7 下一段，默认）/ `conservative`（0.7+0.3）/
        `average`（0.5+0.5）/ `latest_only`（全下一段）。
-   [x] 连续过渡 `continuous`：`alpha = pos / (length - 1)`（按步号 0→1；重叠单步退化为 1.0）。
-   [x] `RTCManager._apply_locked` 按重叠窗口逐位加权（`alpha` 裁剪到 `[0, 1]`，防自定义曲线外推），
        并在 `status().last_chunk.overlap_steps` 上报参与过渡的步数。
-   [x] 配置 / 文档同步：`edge.yml` 注释、`POST /v1/infers/rtc` 字段描述、`server/infer.py` docstring、
        wiki 设计文档、前端面板下拉。
-   [x] 单元用例：连续过渡端到端（重叠 3 步 → 取值 1.0 / 1.5 / 2.0）+ 策略表「权重曲线」语义。
-   [ ] 实机验证：在 dual_piper 上对比 `weighted_average`（固定搭配）与 `continuous`（连续过渡）
        在块边界的动作连续性（观察 `rtc.last_chunk.overlap_steps` 与关节轨迹抖动）。
