# 重力补偿与阻抗控制 实施计划

> **状态**：进行中（分支 `feat/impedance-control`）。方案、取舍与验收判据见
> [设计](../design/robot_pipeline_impedance.md)；本文件只写 **what + how**。

## 摘要

分六个阶段，每个阶段**独立可验收**：**0** 标定工具 → **1** 回归器与拟合 → **2** 重力前馈接入
控制环 → **3** 笛卡尔 K/D 折回关节增益 → **4** 位置型导纳 → **5** 力矩型阻抗（需惯量，远期）。

阶段 0 的产物是「现场能跑的标定脚本 + 原始样本」，**不含拟合**——拟合属阶段 1（需要回归器与
单测）。两者以样本 JSON 为接口。

## TODO

-   [x] **阶段 0-a** `robot/gravity.py`：标定期纯逻辑（位形规划 / 判稳 / 窗口统计 / 摩擦估计 /
        样本汇总 / 存取）+ 单测
-   [x] **阶段 0-b** `scripts/verify_gravity.py`：现场脚本（`--read` 只读自检 / `--hold` 符号单位
        自检 / `--sweep` 采样落盘）
-   [ ] **阶段 0-c** 现场跑数据（**依赖真机**）：`--read` 确认读数可用与开销 → `--hold` 确认符号
        单位 → `--sweep` 出样本；判读「最大 `|τ|` 是否超 ±16 N·m」「每关节行程是否够」「静摩擦
        幅值」
-   [ ] **阶段 1-a** `gravity_regressor(q)`：DH 回归器 `Y(q) ∈ ℝ^{6×n_π}`（每连杆 4 参数）
-   [ ] **阶段 1-b** 单测：点质量解析用例（单连杆 `τ = m·g·l·cos q`）+ 与有限差分对照
-   [ ] **阶段 1-c** `fit_gravity(samples)`：最小二乘（可选岭正则）+ 条件数 / 残差 RMS 报告
-   [ ] **阶段 1-d** `scripts/fit_gravity.py`：样本 JSON → `π̂` + 归档文件（按机型 / 负载命名）
-   [ ] **阶段 2-a** `PiperController.get_motor_states()`：把力矩读数收进控制器（不再由脚本直接
        摸 SDK；数据类含 `q` / `vel` / `tau`）
-   [ ] **阶段 2-b** 配置：机型侧 `robot.gravity`（`enabled` / `alpha` / `t_ff` 上限 / `π̂` 引用）
-   [ ] **阶段 2-c** 控制环注入：`set_joint(q_des, torque_ff = α·τ̂_g(q_meas))`，含
        **限幅 + 异常降级（本拍 `t_ff=0`）+ 告警**
-   [ ] **阶段 2-d** 可观测：`τ_meas` / `τ̂_g` 进观测或 `/v1/health` 扩展字段（现场验收要能看）
-   [ ] **阶段 2-e** 现场验收：`α` 0.2 → 1.0 逐档，看 `k_p·(q_des − q_ss)` 下降曲线
-   [ ] **阶段 3** 笛卡尔 K/D → `Jᵀ K_c J` 折回关节 `k_p` / `k_d`（含奇异处截断 / 正则化）
-   [ ] **阶段 4** 位置型导纳：`τ_ext = τ_meas − τ̂_g` → `J⁻ᵀ` 折力 → 滤波 → 导纳积分 → 修正目标
-   [ ] **阶段 5**（远期）力矩型阻抗：**前置**是可信惯量（厂商 URDF/CAD 或完整辨识），届时才评估
        Pinocchio，并先补「URDF ↔ `PIPER_DH` 一致性」测试
-   [ ] **文档同步**：阶段 2 落地时改 `wiki/design/robot_pipeline_cartesian.md`、
        `robot_pipeline_action_spaces.md`、`robot-pipeline/README.md` 里「本次位姿控制无力矩前馈」
        的措辞，并删掉 `wiki/plan/robot_pipeline_cartesian_plan.md` 里的同名说明

## 阶段 0：标定工具（本次）

### 0-a 纯逻辑 `robot/gravity.py`

只依赖 numpy（现场脚本要能在没有 SDK 的环境里被单测导入）。

-   `T_FF_LIMIT_NM = 16.0`（V188 12-bit，全关节）、`GRAVITY_VECTOR`；
-   `plan_waypoints(q_home, span, limits, combined, seed)`：逐关节 ±`span`（**一次只动一个关节**，
    风险最小）+ 可选多关节组合（固定种子）；**越界的一律丢弃并回报**；
-   `SettleDetector`：连续 K 拍满足 `|velocity| < εv`、`|Δq| < εq`、`|Δτ| < ετ` → 判稳；
-   `window_stats(records)`：窗口均值 + 标准差 + 速度峰值；
-   `friction_from_passes(forward, backward, max_distance)`：正反两遍按关节角最近配对 →
    `|Δτ|/2` 作为库仑摩擦幅值估计；
-   `summarize_samples(samples)`：最大 `|τ|`（含关节号）、是否超 ±16、每关节行程、力矩标准差；
-   `save_samples` / `load_samples`（JSON 往返）。

### 0-b 现场脚本 `scripts/verify_gravity.py`

三种模式，**默认只读**：

| 模式             | 做什么                                                                                  | 会不会动                               |
| ---------------- | --------------------------------------------------------------------------------------- | -------------------------------------- |
| `--read`（缺省） | 只读 N 秒：每位关节 `q` / `velocity` / `torque` 的均值与标准差、单次读取耗时            | 不发送任何指令                         |
| `--hold`         | 以**当前实测位置**为目标持续下发 → 判稳 → 逐关节比对 `τ_meas` 与 `k_p·(q_des − q_meas)` | 不下发位移（只维持当前位置）           |
| `--sweep`        | 按位形规划扫描，正反两遍，判稳后取窗口均值，落盘样本 JSON                               | **会运动**（请无人、无负载、急停可达） |

要点：30 Hz 持续重发（`move_mit` 是直通无平滑）；力矩按关节取
（`get_motor_states(i).msg.torque`，1-based）；`Ctrl+C` 与异常都不丢已采样本（`finally` 回原位 +
落盘）。

## 阶段 1：回归器与拟合

-   `gravity_regressor(q)`：由 `PIPER_DH` 推出（连杆质心位置对关节角的偏导），参数按
    `π_i = (m_i, m_i·c_i)` 排列 → `Y(q)`；**与 FK / IK / 限幅共用同一份运动学**；
-   单测：单连杆点质量解析式对齐（符号约定由它钉住）+ 与数值偏导对照；
-   `fit_gravity(samples)`：`min‖Y π − τ‖²`（可选岭正则），输出 `π̂` + 条件数 + 残差 RMS；
-   `scripts/fit_gravity.py`：读样本 JSON → 写 `π̂` 归档（含元信息：机型 / 负载 / 时间 / 增益）。

## 阶段 2：接入控制环

-   `PiperController` 增 `get_motor_states()`（返回 `q` / `vel` / `tau` 数据类），控制环每拍取一次；
-   `BaseRobot` 子类在 `_apply_action()` 里算 `τ_ff = α · τ̂_g(q_meas)` 并透传 `set_joint(..., torque_ff=)`；
-   **降级规则**：读数 `None` / 超时 / NaN / 参数域外 → 本拍 `t_ff = 0` + 限流告警，不中断控制；
-   限幅：`|τ_ff| ≤ min(16 N·m, 配置上限)`；
-   观测：把 `τ_meas`、`τ̂_g`（可选 `α`）带进 `/v1/health` 扩展或采集元信息，供现场验收。

## 阶段 3 / 4 / 5

-   **3**：`K_q = Jᵀ K_c J`（`D_q` 同理）→ 覆盖 `MIT_CTRL_CFG` 的 `k_p` / `k_d` 下发；`k_p` 合法域
    `[0, 500]`、`k_d` `[−5, 5]`，需截断 + 近奇异正则化；
-   **4**：导纳环（`F_ext` 估计 → 一阶 / 二阶导纳 → 修正 `x_des`），带宽受 30 Hz + 滤波限制，
    需现场标定可达到的最软 `K_c`；
-   **5**：力矩型（`k_p → 0`）——失去固件位置安全网，且需要 `M(q)` / `C(q,q̇)`，**前置**是惯量来源。
