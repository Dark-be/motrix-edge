# robot-pipeline 统一坐标系与外参实施计划

设计：[robot-pipeline 统一坐标系与外参（frames）](../design/robot_pipeline_frames.md)。

## TODO

### 文档

-   [x] 设计文档 `wiki/design/robot_pipeline_frames.md` + `design/index.md` 登记
-   [x] 交叉引用同步：`robot_pipeline_depth.md`（Phase 2 指向本文）、`motrix_edge_server.md`（`/v1/depth`
        坐标字段）、`motrix_edge_adapter.md`（`/v1/cameras` 外参段）、`robot-pipeline/README.md`

### 帧模型（edge 侧，纯 numpy）

-   [x] `src/motrix_edge/geometry/transforms.py`：`rpy` ↔ 矩阵、组合 / 求逆、点变换、16 数平铺校验
        （约定与 `robot/kinematics/transforms` 一致：`R = Rz·Ry·Rx`，万向锁 `yaw = 0`）
-   [x] `src/motrix_edge/geometry/extrinsics.py`：`FrameSet`（`version` / `world` / `arms` / `cameras`）
        读写与校验 + `world_from_camera()`（腕相机用同拍位姿合成）
-   [x] `src/motrix_edge/geometry/deproject.py`：像素 + 深度 → 相机系 XYZ
-   [x] `tests/test_geometry.py`：往返 / 组合 / 校验失败 / 腕相机合成 / 与 `robot.kinematics.transforms`
        的 `rpy` **交叉一致**（防止两套实现漂移）

### 标定求解（robot 侧，纯 numpy + cv2 检测）

-   [x] `robot-pipeline/src/robot/calibration/solver.py`：点集配准（Umeyama，无缩放）、`AX = XB`
        （Kronecker 零空间 + 旋转正交化，规避 OpenCV 5 移除 `calibrateHandEye`）、探针交替最小二乘
        （`T_base_board` + `t_probe`）、虚拟数据自测
-   [x] `robot-pipeline/src/robot/calibration/board.py`：ChArUco 板构造 / 检测 / `solvePnP` → `T_cam_board`
-   [x] `robot-pipeline/src/robot/calibration/samples.py`：样本 JSON 读写与结构校验
-   [x] `robot-pipeline/src/robot/calibration/pipeline.py`：由样本组装视图 / 触碰 → `solve_frames()`
        （条件数、位姿角展度、固定相机散布等可用性判据）
-   [x] `robot-pipeline/src/robot/calibration/store.py`：`<根>/config/calibration/frames.json` 读写
        （不播种；不合法 → 整份忽略 + 一条 WARNING，坐标功能静默降级）
-   [x] `tests/test_robot_calibration.py` / `tests/test_robot_calibration_pipeline.py`：虚拟 `AX = XB`
        复现、探针求解复现、合成板投影 → PnP 复现、三相机一致性、样本 / 产物读写与校验失败路径

### 运行期接线

-   [x] `adapter/http_contract.py`：相机字段常量（`mount` / `arm` / `frames`）+ `/v1/depth` 坐标字段
        （`xyz_camera` / `xyz_world` / `frame` / `world`）+ `MOUNT_VALUES` 取值
-   [x] 机器人：`BaseRobot.camera_mount()` / `WRIST_CAMERAS` 声明装配事实，`contract_server` 的
        `/v1/cameras` 按 `{cameras, frames}` 收口
-   [x] Edge：`adapter.frame_set()` 暂存产物，`server/depth.py` 增 `xyz_camera` / `xyz_world` /
        `frame` / `world`（无外参 → null，不报错；腕相机用同拍位姿）
-   [x] 测试：`tests/test_robot_depth.py`（装配事实段）/ `tests/test_server.py`（`/v1/depth` 坐标字段：
        有外参 / 无外参 / 无深度 / 腕相机缺位姿）

### 工具

-   [x] `robot-pipeline/scripts/calibrate_extrinsics.py`：`--collect`（碰硬件）/ `--solve`（纯离线，
        含 `--install`）/ `--self-test`（虚拟数据）
-   [x] `robot-pipeline/scripts/verify_extrinsics.py`：跨相机一致性 + 与左臂触碰值的绝对精度（走
        Edge HTTP，验的是运行期那一条通路）

### 外部数据导入（别人的采集工具 + 只采了一条臂）

-   [x] `robot-pipeline/src/robot/calibration/external.py`：外部会话读取（manifest / sample /
        factory 内参）+ 单臂 `world` 锚定（镜像 + 同向；`vertical` / `camera` 两种对称面）
-   [x] `robot-pipeline/scripts/import_external_frames.py`：CLI（`--root` 自动探测）+ 判读 +
        `--out` / `--install`
-   [x] `tests/test_robot_calibration_external.py`：虚拟外部会话端到端复现（腕相机 / 固定相机 /
        锚定）+ 锚定数学 + 外部格式失败路径
-   [x] 设计文档「外部数据导入（跨工具）」章节 + 分层表一行 + 「未做」补实测替代
-   [x] 随仓库分发实测产物：`robot-pipeline/src/config/calibration/frames.json` + `store` 改走
        `resolve_config_file`（本地优先 / 缺失只读回落 + 一条可追溯 WARNING）
-   [ ] **左臂探针碰板实测**（代替装配假设；要 mm 级必须做，现场 10 分钟）

### 校验

-   [x] `ruff format` / `ruff check` / `npx unified-ci add-header --check` / `pytest -q`
        （730 passed / 1 skipped；`prettier` 只对本 MR 改动的 md 跑，`wiki/` 既有未格式化文件不在本次范围）
-   [x] 产物与脚本的「缺文件 / 非法文件」路径手工复核（坐标功能应静默关闭，其余链路不变）

### 现场（需硬件，不在本 MR 内完成）

-   [ ] 按设计文档 Step 0–4 实机标定 + 换位置验收，把 RMS 记到本文「验证记录」
-   [ ] 若残差偏大：用 `cv2.calibrateCamera` 复核内参、检查板固定与探针尖对准

## 验证记录

（待现场标定后补齐：日期 / 机型 / 各相机 RMS / 跨相机一致性 / 绝对精度。）

### 外部数据（别人工具采的，2026-10-08 两份会话）

来源：`piper_camera_calibration`（右臂，ChArUco 6×6 / 0.03 / 0.022 / `DICT_5X5_100`，
RealSense 出厂内参、整流图）。**全部量都是我们自己的求解链重算的**（不读对方的 `handeye.yaml`）：

| 项                                                              | 值                                                                                                                                                                                           |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 前提 1：我们的 `PiperKinematics.fk(q)` vs 对方 `T_base_gripper` | 位置 max 0.092 mm · 姿态 0.0000° ⇒ **同一法兰帧**                                                                                                                                            |
| 前提 2：我们的 `detect()` 读对方图片                            | id 集合逐帧一致 · 角点像素差 0.000 px                                                                                                                                                        |
| 腕相机手眼（`session_type=handeye`，30 样本）                   | 29 对 · 姿态铺开 160.9° · 残差 1.366° / 5.20 mm · PnP RMS 0.159 px                                                                                                                           |
| （交叉校验）对方 daniilidis                                     | RMSE 0.87° / 6.87 mm（holdout 8.94 mm）——两解互差 1.17° / 2.0 mm ⇒ **误差由数据定，不是算法**                                                                                                |
| 固定相机（`session_type=head_via_wrist`，30 帧静止板）          | PnP RMS 0.174 px · 板位姿帧间离散 0.553° / 1.97 mm                                                                                                                                           |
| （交叉校验）我们的 `T_base_head` vs 对方                        | 0.012° / 0.12 mm                                                                                                                                                                             |
| 左臂锚定（装配假设：同向 + 垂直对称面）                         | 两基座间距 **642 mm** · 高度差 0 mm · `T_world_base_right` 平移 `(-0.0005, -0.6416, 0.0)` m                                                                                                  |
| 随仓库分发的产物                                                | `robot-pipeline/src/config/calibration/frames.json`（`cam_head` fixed + `cam_right_wrist` / `cam_left_wrist` wrist；无 `probe_tip`）⇒ clone 后 `/v1/depth` 直接给 `xyz_world`                |
| 左腕相机（`cam_left_wrist`）                                    | 外参**继承自 `cam_right_wrist`**（`--inherit-left-wrist`，同件同向假设）⇒ 不写 `rms_m`、**无实测残差**；离线合成已验证出 `xyz_world`（`arms.left=I` · `FK_left(obs.pose)` · `T_flange_cam`） |
| Web 控制台                                                      | 深度卡片增 `xyz_camera` / `xyz_world` 与帧名展示，卡片移到机器人命令下方（控制台目录不入本仓版本，仅本地生效）                                                                               |
| ⚠️ 未做                                                         | 跨相机一致性验收（需 Edge + 租约在线）· 左臂实测锚定 · **左腕外参的现场证伪（两路原图比朝向 / 同一物理点两路查坐标）或 head 当桥实测**                                                       |
