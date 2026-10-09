# robot-pipeline 统一坐标系与外参（frames）

## 摘要

深度查询（[深度观测](./robot_pipeline_depth.md) Phase 1）回答的是「这个像素有多远」；本期把它接到
**统一基底**上：一个**单一 `world` 帧** + 每条链的固定外参，使「同一物理点」被任意相机看到、被任意臂
触碰到时，都能给出**同一个坐标系下**的坐标。

-   **基底选谁**：`world` ≡ **左臂基座**（`left` 臂 `fk()` 的 `T_0_0` 原点系，j1 轴心处）。
    左臂的位姿观测 / 位姿动作本来就原生在这个系里（恒等，省一个外参），右臂与所有相机都锚到它。
-   **谁持有**：帧模型 / 变换 / 反投影的单点在 `motrix_edge.geometry`（纯 numpy，**无硬件、无
    OpenCV**，edge 运行期与标定工具共用）；标定产物、求解器与工具在 `robot-pipeline`。
-   **怎么到 edge**：外参是**静态元数据**，由机器人进程经 `GET /v1/cameras` 上报（与内参 /
    `depth_scale` 同一通道）——**edge 不读机器人配置**。
-   **可关**：没有标定产物 → 坐标字段一律 `null`，其余链路（深度 / 观测 / 采集）完全不变。

> **状态**：随「统一坐标系与外参」MR 引入。**现场标定与验收未做**（需硬件），流程见本文「标定流程」。

## 目标与约束

-   **单一基底**：对外只暴露一个坐标帧 `world`；内部所有坐标（深度反投影、臂位姿、标定量）都在它
    之下表达，避免「每个模块一套系」。
-   **一致性口径**（重要）：三台相机内参 / 视场不同，**不存在「同一个像素」**——同一物理点的像素坐标
    必然不同。要保证的是：同一物理点被不同相机各自反投影后，**在 `world` 下的坐标互相接近**。
    验收指标见「标定流程 · 验收」。
-   **单位 / 姿态约定**（与既有位姿契约同源）：长度米、角度弧度；`rpy = [roll, pitch, yaw]`，
    `R = Rz(yaw)·Ry(pitch)·Rx(roll)`（`robot/kinematics/transforms` 同一约定，含万向锁 `yaw = 0`）。
-   **变换的存储形状**：4×4 齐次变换，JSON 里用 **16 个浮点数按行主序**（`row-major`）平铺。
-   **零新增依赖**：求解器纯 numpy（与 `solve_ik` / `fit_gravity` 同风格）；检测与 PnP 用
    robot-pipeline 已有的 `opencv-python-headless`。⚠️ **不用** `cv2.calibrateHandEye` /
    `calibrateRobotWorldHandEye`——本机实测 OpenCV **5.0.0 已移除**这两个绑定，押在它上面会在现场
    直接报 AttributeError。
-   **不动 Phase 1 契约**：深度仍是对齐到彩色图的 `uint16`；内参仍只有彩色一套；`/v1/depth` 的
    现有字段（`depth_m` / `valid` / `intrinsics` / `depth_scale`）语义不变，本期**纯增量**。
-   **不改控制链路**：外参只用于「坐标表达」，不下发、不参与限速 / 到位判定。

## 帧体系

| 帧                         | 含义                                                                  | 谁给的                 |
| -------------------------- | --------------------------------------------------------------------- | ---------------------- |
| `world`（= `left_base`）   | 左臂基座系（`fk(q)` 的原点 / 轴向）——**唯一对外基底**                 | 机械臂 DH 模型（恒等） |
| `base_left` / `base_right` | 各臂基座系（右臂由 `T_world_base_right` 锚到 `world`）                | 标定（左臂恒等）       |
| `flange_<arm>`             | 各臂法兰系 = `FK(q)` 的像（`observations/pose` 就是它）               | 运动学（运行期）       |
| `cam:<name>`               | 相机光学系（RealSense：`x` 右 / `y` 下 / `z` 前，右手系）             | 标定（外参）           |
| `board`                    | 标定板系——**只用于标定流程**，不是对外帧                              | 标定板几何             |
| `table`                    | 工作台面派生帧（可选，`T_world_table`）——只有按台面表达任务点时才需要 | 标定（可选）           |

两条必须记住的约定：

1.  **光学系 ≠ 机械臂系**：RealSense 光学系 `x` 右 / `y` 下 / `z` 前，机械臂基座系 `z` 向上。
    这两者之间的固定旋转是**约定**（不是标定结果），由 `motrix_edge.geometry.OPTICAL_TO_ROBOT`
    写死单一来源，任何标定产物都在**机器人轴向约定**下表达（`z` 向上、右手系）。
2.  **腕相机随动**：腕相机只能标「法兰 → 相机」这个常数，运行期必须合成：

    $$T_{world\leftarrow cam} = T_{world\leftarrow base} \cdot FK(q) \cdot T_{flange\leftarrow cam}$$

    其中 $FK(q)$ 来自**同一拍**的 `observations/pose`（实测位姿）——跨拍合成等于把臂的运动当成
    外参误差。

## 契约

### `GET /v1/cameras`（机器人进程 → Edge，新增装配事实 + 外参产物）

```json
{
    "cameras": [
        {
            "name": "cam_head",
            "width": 640,
            "height": 480,
            "intrinsics": { "fx": 605.1, "fy": 604.9, "cx": 320.5, "cy": 240.6 },
            "depth": { "scale": 0.001, "aligned_to_color": true },
            "mount": "fixed",
            "arm": null
        },
        {
            "name": "cam_left_wrist",
            "mount": "wrist",
            "arm": "left"
        }
    ],
    "frames": null
}
```

-   `mount` / `arm` 是**装配事实**（类常量 `WRIST_CAMERAS`），不依赖标定产物——未标定时调用方也该
    知道「这路相机随臂动」；固定相机的 `arm` 为 `null`。
-   外参**不拆进相机条目**，而是由 `frames` 一次性给整份产物（非 `null` 时就是下节那份 JSON 原样）：
    产物要整体校验（`version` + 腕相机的 `arm` 必须在 `arms` 里 + 旋转正交），拆开就得在多处重复
    同一套校验，schema 也只能有一份。
-   未标定 / 产物非法 → `frames` 为 `null`（键仍在）；`mount` / `arm` 照常给，调用方据此判断，不必猜。
-   静态元数据：Edge 侧惰性缓存一次（沿用既有 `camera_infos()`），**标定后需重启机器人进程**才生效
    （与内参同款口径）。

### `GET /v1/depth`（Edge，新增坐标字段）

| 字段         | 内容                                                                 |
| ------------ | -------------------------------------------------------------------- |
| `xyz_camera` | 该像素在**相机光学系**下的 `[x, y, z]`（米）；无外参 / 无深度 → null |
| `xyz_world`  | 同一点在 `world` 下的 `[x, y, z]`（米）；无外参 / 无深度 → null      |
| `frame`      | `xyz_world` 的帧名（`"world"`）；不可用 → null                       |
| `world`      | `world` 帧的**别名**（`"left_base"`）；不可用 → null                 |

-   反投影（内参来自同一相机的彩色内参，深度已对齐到彩色图）：

    $$X_{cam} = \Big[\tfrac{u_{px}-c_x}{f_x}\,z,\ \tfrac{v_{px}-c_y}{f_y}\,z,\ z\Big]$$

-   无外参时**不报错**：`xyz_*` 为 `null`，`depth_m` 照常给——深度查询本身不依赖外参。
-   腕相机用**当前最新帧**的 `observations/pose` 合成（与深度同源同一帧缓存）；缺位姿 → `xyz_world`
    为 null、`xyz_camera` 照常给。

## 标定产物

落点 `<根>/config/calibration/frames.json`（`<根> = $MOTRIX_ROBOT_PIPELINE_DIR`，缺省
`<cwd>/motrix-robot-pipeline`）；走 `config.resolve_config_file` 口径（`gravity/*.json` 同款）：
**本地产物优先；本地没有副本 → 只读回落包内随仓库分发的那一份**（`src/config/calibration/frames.json`），
不写盘、不播种，并打一条带 `tool` / `calibrated_at` 的 WARNING——「当前用的不是本机产物」必须看得见。
包内**从不**放占位 / 零值外参（一份假的外参比没有外参危险得多）。

> 仓库里现在**就带一份实测产物**（2026-10-09，右臂 + `external-mirror` 锚定，见
> [外部数据导入](#外部数据导入跨工具)）：实机 clone 下来不改配置就能读到 `xyz_world`。
> ⚠️ 它是**台位专有**的：换台位 / 动过相机 / 换了相机支架后必须重标（`--install` 写本机副本），
> 否则坐标会按旧台位算；`effective_path()` 与 `/v1/cameras` 的 `frames` 段可查当前用的是哪一份。
>
> **左腕相机（`cam_left_wrist`）的外参是「从右腕继承」的**（`inherit_wrist_camera`：照搬
> `T_flange_cam`、不写 `rms_m`）——前提是两臂腕相机**同一支架件、相对各自法兰同向安装**。
> 法兰系是**随臂的局部系**，故与两臂怎么摆 / 底座是否镜像无关；但两种情形会破：支架是镜像件
> （或同一件翻面装）→ 相差一个轴向翻转；同件但装配角差几度 → 几度 / 几 cm 量级偏差。
> **证伪（现场 1 分钟）**：两路原图对着同一场景比对朝向（一致 = 同向；互为镜像 = 镜像件），或让
> 同一物理点同时出现在两路画面里各查一次 `xyz_world`（mm–cm 内一致才对，米级差异/方向翻转
> 就是假设破了）。**实测（10 分钟，推荐）**：板不动、head 与左腕同拍、左臂摆 3–6 个位姿 →
> `X = T_flange_board · T_board_cam` 逐帧平均（`T_base_left_board = T_base_left_head · T_head_board`），
> 不需要 `AX = XB`。

```json
{
    "version": 1,
    "world": "left_base",
    "rpy_order": "zyx",
    "length_unit": "m",
    "angle_unit": "rad",
    "calibrated_at": "2026-10-09T12:00:00",
    "tool": "probe",
    "arms": {
        "left": { "T_world_base": [16 个数] },
        "right": { "T_world_base": [16 个数] }
    },
    "cameras": {
        "cam_head": { "mount": "fixed", "T_world_cam": [16 个数], "rms_m": 0.0021 },
        "cam_left_wrist": { "mount": "wrist", "arm": "left", "T_flange_cam": [16 个数], "rms_m": 0.003 },
        "cam_right_wrist": { "mount": "wrist", "arm": "right", "T_flange_cam": [16 个数], "rms_m": 0.0028 }
    },
    "table": null
}
```

-   `arms.<arm>.probe_tip`：探针尖在**法兰系**的位置（米，**可选**）——标定副产品；
    `verify_extrinsics.py` 的绝对精度比对要用它。

校验（读到文件就全量校验，**半份产物不如没有**）：`version` 支持、`world` 非空、每个变换是 16 个数
且旋转部分正交（`RRᵀ = I`、`det = +1`，容差 1e-6）、腕相机的 `arm` 在 `arms` 里存在、
`mount` 取值合法。不合法 → **忽略整份产物**并打一条 WARNING（坐标功能视为不可用），不半途生效。

## 标定流程

⚠️ 全程**静止采样**：三台相机各自 `wait_for_frames()`、**没有跨相机硬件同步**，运动中采样会把时序
误差当成标定误差（腕相机最严重）。

### Step 0 · 准备

-   硬件：ChArUco 板（打印在硬基板上，`square_length` / `marker_length` 量准）+ 探针（3D 打印
    尖头，夹在法兰上）；可选 Ø20 钢球（交叉校验）。
-   状态：**退出会话并撤销租约**（标定要动臂）、**关遥操作**、**开重力前馈**（若已标定 → 减小 MIT
    稳态误差，提升探针触碰精度）。
-   板放在工作区中心（三台相机都能看到、两臂都能碰到）；**标定全程板不动**（相机看板那一步与
    探针触碰那一步必须指的是同一块、同一位置的板——探针给的是「这块板」的位姿）。

### Step 1 · 采集

```bash
PYTHONPATH=src python scripts/calibrate_extrinsics.py --collect --out /tmp/frames_samples.json
```

1.  **相机 ↔ 板**：每台相机 10–15 帧。固定相机（`cam_head`）**板不动、静止多拍**（多帧只用于平均
    降噪——PnP 单帧就是 6 自由度，而「挪板再拍」会让探针那一步得到的板位对不上）；腕相机**板不动、
    臂摆 10–15 个位姿**（板始终在视野内、朝向尽量分散），每位姿同时记 **图像 + 该臂
    `observations/pose`**；
2.  **臂 ↔ 板（探针）**：尖探针夹在法兰上，两臂各自用探针尖触碰板上 4–6 个**已知角点**，每点记录
    「碰的是哪个角点 + 该臂当前位姿」。

### Step 2 · 解算（纯离线，任意机器可跑）

```bash
python scripts/calibrate_extrinsics.py --samples /tmp/frames_samples.json --solve
python scripts/calibrate_extrinsics.py --samples /tmp/frames_samples.json --solve --install
```

1.  每帧 ChArUco 检角 + `cv2.solvePnP`（内参用 `/v1/cameras` 的出厂值）→ `T_cam_board`；
2.  腕相机：多姿态下同一板 → 解 $AX = XB$ 得 `T_flange_cam`（Kronecker 零空间 + 旋转正交化，
    纯 numpy——OpenCV 5.0.0 已移除 `calibrateHandEye`）；
3.  臂 ↔ 板：探针数据**交替最小二乘**求 `T_base_board` 与探针尖在法兰系的位置 `t_probe`
    （探针不需要预先标定——它与臂外参一起解出来）；
4.  换算到 `world`：统一用 `T_world_base(arm) = T_world_board · T_base(arm)_board⁻¹`（world 臂代进去
    自然得到恒等——它**就是** world）；固定相机 `T_world_cam = T_world_board · T_board_cam`（PnP 给的
    是反向的 `T_cam_board`，**要取逆**——链式组合必须逐级相接）；腕相机直接得 `T_flange_cam`。

判读（决定这次标定能不能用）：每项的 **RMS** 与**逐帧残差最大者**都会打印——RMS 明显偏大（> 5 mm）
通常是「板没固定好 / 采样时被碰 / 运动未停稳」，看最大残差定位是哪一帧；`T_board_cam` 的残差还能
暴露「板没被填满视野 / 角点模糊」。

### Step 3 · 安装

`--install` 写 `<根>/config/calibration/frames.json`；**标定后重启机器人进程**（外参随
`GET /v1/cameras` 上报、Edge 侧缓存），再重启 Edge 或等其缓存重建。

### Step 4 · 验收（必做，且**换位置**做）

```bash
python scripts/verify_extrinsics.py --host 127.0.0.1 --port 8000 --lease <lease_id>
```

| 指标         | 含义                                                         | 目标                       |
| ------------ | ------------------------------------------------------------ | -------------------------- |
| 跨相机一致性 | 同一物理点被两台 / 三台相机各自反投影到 `world` 后的两两距离 | **RMS < 5 mm** @0.5–1 m    |
| 绝对精度     | 与左臂探针触碰值（`FK` 的 `world` 坐标）比对                 | 5–15 mm（含 MIT 稳态误差） |

⚠️ 只在板附近测 = 自证：必须换 2–3 个区域各测一遍。跨相机一致性**主要**由 Step 2.1（相机 ↔ 板）决定；
臂的 `FK` 误差是**共模**的（一起平移，不会让三台相机互相不一致），所以不要让相机间相对外参经过
机械臂传递。

### Step 5 · 什么时候必须重标

碰 / 拧过相机或相机支架、臂拆装或撞过、换镜头 / 改分辨率 / 关掉 depth-color 对齐、换了标定板。
温度漂移不用管。

### Step 6 · 离线自测（上真机前先跑）

```bash
python scripts/calibrate_extrinsics.py --self-test
```

用**已知外参生成的虚拟数据**（合成板投影 + 合成探针触碰）跑完整求解链，必须复现真值（误差 < 1e-6）。
这一步专门拦「约定错」——`AX = XB` 的 A/B 取法、手性、`rpy` 顺序、行/列主序，写错了真机上是
「看着像标定误差」的鬼故事。

## 外部数据导入（跨工具）

现场采集未必出自我们的 `calibrate_extrinsics.py`（本次实录来自 `piper_camera_calibration`）。
只要样本里有「角点 + 位姿 + 板参 + 内参」，就能**用我们自己的求解链重算**（不读对方的
`handeye.yaml`）：

```bash
python scripts/import_external_frames.py --root <外部数据根> [--out frames.json | --install]
```

| 外部字段                                 | 我们的用法                                                                                          |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `manifest.json` 的 `board`               | `BoardSpec`（**不套我们的缺省板参**；实录 6×6 / 0.03 / 0.022 / `DICT_5X5_100`）。                   |
| `samples/*/charuco.ids` + `corners_xy`   | `pose_from_corners()` → `T_cam_board`（**不重检图像**：同一套 ChArUco 约定，实测逐点差 0.000 px）   |
| `samples/*/robot.T_base_gripper`         | `T_base_flange`（实测与我们的 `PiperKinematics.fk()` **同帧**：差 0.09 mm / 0.0000°）               |
| `factory/<相机>.yaml` 的 `camera_matrix` | 彩色内参（整流图、零畸变 → 与 PnP 假设一致；畸变非零直接报错）                                      |
| `session_type=handeye`                   | 腕相机：`solve_hand_eye` → `T_flange_cam`；`T_base_board` = 逐帧 `T_base_flange·X·T_cam_board` 平均 |
| `session_type=head_via_wrist`            | 固定相机：板静止多帧平均 → `T_cam_board`；`T_base_head = T_base_board · T_cam_board⁻¹`              |

### 只采了一条臂时怎么锚 `world`

外部数据（本次）只有右臂，而 `world` 取左臂基座——左右基座的关系在这份数据里**没有观测**。
`symmetric_anchor()` 用「左右臂相对 head 相机对称 + 两底座坐标系同向」推位置：

$$T_{world\leftarrow base_R} = \Big[\,I \;\Big|\; -2\,(o_c \cdot n)\,n\,\Big]$$

（`n` = 对称面法向、`o_c` = head 相机光心在右基座系下的位置；旋转为 `I` 就是「同向」。）

-   `--mirror-axis x|y`：哪根相机轴是横向（对称面法向）；
-   `--plane vertical|camera`：`vertical`（默认）把 `n` 投影到水平面，满足「两臂同底板 ⇒ 基座同高」
    这条**已知物理事实**，消掉相机安装倾斜漏进的位置残差；`camera` 原样用相机轴向（残留倾斜，
    实录 22.6°）。

⚠️ 风险账（本次数据的实测值）：两基座**位置**能由对称假设给出（实录 642 mm、同高 ✓），但
「同向 / 绕竖直 180°」选错是**米级**偏差；对称面未过相机光心时，横向偏 10 mm → 左基座偏 20 mm；
法向偏 1° → 偏 ~11 mm。⇒ 这条路线是 **cm 级且无法自证**，要 mm 级必须实测（左臂探针碰同一块板
→ `solve_probe` → `T_base_left_board`，与 `T_head_board` 一合成即可，**不需要重采右臂**）。
产物 `tool` 字段写死 `external-mirror`，事后可追溯。

## 分层与归属

| 层                     | 位置                                                                   | 职责                                                                                                         |
| ---------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| 帧模型 / 变换 / 反投影 | `src/motrix_edge/geometry/`                                            | 纯 numpy：`rpy` ↔ 矩阵、组合 / 求逆、`frames.json` 读写与校验、像素 → `world`；**edge 运行期与标定工具共用** |
| 标定求解               | `robot-pipeline/src/robot/calibration/`                                | 纯 numpy：`AX = XB`、点集配准（Umeyama）、探针交替最小二乘、PnP / ChArUco 检测（cv2）、样本 JSON、产物读写   |
| 外部采集导入           | `robot-pipeline/src/robot/calibration/external.py`                     | 读外部工具的采样格式 + **单臂采集的 `world` 锚定**（装配假设；见上节）                                       |
| 运行期外参上报         | `robot-pipeline/src/robot/base_robot.py` + `server/contract_server.py` | 把产物整成契约形状（键名单点在服务边界收口）                                                                 |
| 运行期坐标查询         | `src/motrix_edge/server/depth.py`                                      | 反投影 + 用同拍位姿合成腕相机外参                                                                            |

## 与既有的关系

-   [位姿动作（求解器）](./robot_pipeline_cartesian.md)：`FK` = `基座 → 法兰`，`observations/pose`
    就是这个变换的 `[xyz, rpy]`——本期把它接上 `world`，模型不变。
-   [深度观测（depth）](./robot_pipeline_depth.md)：Phase 1 的数据模型（对齐深度 + 彩色内参 +
    `depth_scale`）正是 Phase 2 的全部输入，本期**未改**任何 Phase 1 契约。
-   帧声明先例：`POSE_FRAME` / `env.get_env_meta.pose_frame`（「读与写必须同系」）——`world`
    沿用同一套口径：**数值 + 帧名一起给**，调用方不必猜。

## 未做 / 未决

-   **现场标定与验收**（需硬件）：本文只落地模型、工具与运行期接线。
-   **左臂锚定的实测替代**：`external.py` 目前只给「装配对称假设」这一条（cm 级、无法自证）；
    实测版（左臂探针碰同一块板 → `T_base_left_board` + `probe_tip`）**未实现**，需要现场先补
    一次左臂碰板采集。
-   **跨相机时间同步**：运动中的「同一物理点」一致性受「三台相机不同拍」限制；要做得上触发 / 硬件
    同步，属另一件事。
-   **相机内参自标**：先用 RealSense 出厂内参（`GET /v1/cameras`）；标定残差偏大时再用
    `cv2.calibrateCamera` 复核。
-   **点云 / 区域查询**：本期只做单点；整帧端点另行评估。
-   **`table` 派生帧**：产物里留了 `table` 字段位（`null`），按台面表达任务点时再启用。
