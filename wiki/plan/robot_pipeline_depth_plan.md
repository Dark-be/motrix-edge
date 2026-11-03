# robot-pipeline 深度观测 实施计划

方案见 [robot-pipeline 深度观测（depth）](../design/robot_pipeline_depth.md)。本期只做
**拿到深度**（Phase 1），不做坐标转换。

## TODO

### 契约（两端单点）

-   [x] `adapter/shm_contract.py`：布局 v6 → **v7**（header 增 `depth_count` / `depth_width` /
        `depth_height` / `depth_offset` / `depth_data_size`，区域尾部加 `depths`），writer / reader
        支持深度区；版本不符的报错文案保持「两端须同步升级」
-   [x] `adapter/base.py`：`DEPTH_PREFIX` / `depth_names_of()` / `RobotAdapter.DEPTH_CAMERAS`
        （类常量、能力声明面）+ `camera_infos()` / `depth_camera_names()`
-   [x] `adapter/http_contract.py`：`PATH_CAMERAS` + 相机元数据字段常量（含 `INTRINSICS_KEYS`）
-   [x] `robot-pipeline/src/robot/base_robot.py`：`DEPTH_PREFIX` / `DEPTH_CAMERAS` /
        `depth_camera_names()` / `camera_meta()` / `robot.depth` 配置解析 + `capture_frames()`

### 机器人端（数据生产）

-   [x] `sensor/realsense_sensor.py`：`enable_depth` + `rs.align(color)` + `depth_scale` +
        彩色内参（启动时取一次）+ `camera_info()`
-   [x] `robot/dual_piper_robot.py`：三路开深度（按配置）、`DEPTH_CAMERAS`、取帧带深度
-   [x] `robot/dual_alicia_piper_robot.py`：仅 `cam_head` 开深度
-   [x] `robot/test_robot.py` + `sensor/test_vision_sensor.py`：合成深度（`depth_mm = 1000 + u`）
-   [x] `server/contract_server.py`：`_ShmPublisher` 写深度区、`GET /v1/cameras`（键名按契约
        常量收口）、`observation_keys` 增深度键、`/observe` 回显深度形状与范围
-   [x] `src/config/{dual_piper,dual_alicia_piper,test_robot}.yml`：`robot.depth` 段

### Edge 侧（消费 + 查询）

-   [x] `adapter/http_shm_adapter.py`：读深度区（严格 count 校验）、观测量
        `observations/depth/<cam>`、`GET /v1/cameras` 惰性缓存 + `camera_infos()`
-   [x] `adapter/{test_adapter,dual_piper_adapter}.py`：`DEPTH_CAMERAS` 能力声明
-   [x] `server/depth.py`：`DepthService`（租约校验 → 归一化坐标 → 源像素 → 米）
-   [x] `server/routes/depth.py` + `routes/__init__.py` 注册 + `deps.Services.depth`
-   [x] `server/app.py`：`create_app(depth=...)`；`__main__.py`：构造并注入（生产入口）
-   [x] `server/preview.py`：`observation.depth` = 可用深度相机名
-   [x] `frame/__init__.py`：文档说明深度原样透传（不降采样、不进 JPEG）

### 测试与校验

-   [x] `tests/test_shm_contract.py`：v7 往返 + 深度区（含 `depth_count = 0`、形状不符拒绝）
-   [x] `tests/test_server.py` + `tests/fake_robot.py`：`/v1/depth`（正常 / 0 像素 / 未知相机 /
        越界坐标 / 租约三态 / 未注入 501）
-   [x] `tests/test_dual_piper_adapter.py`：深度读取 + 元数据缓存 + 路数不符丢弃 + 相机裁剪跟随
-   [x] `tests/test_robot_depth.py`：相机声明 / 配置解析 / 取帧组装 / 相机元数据（离线虚拟机器人）
-   [x] `tests/test_robot_pipeline_configs.py`：`robot.depth` 段形状
-   [x] ruff check/format + `pytest -q` = **662 passed / 1 skipped**

### 文档

-   [x] `wiki/design/index.md` 登记设计文档
-   [x] `wiki/design/{robot_pipeline_action_spaces,robot_pipeline_runtime,motrix_edge_adapter,
motrix_edge_server,motrix_edge_frame_webrtc,motrix_edge_primitives}.md` 交叉引用 / 契约同步
-   [x] `robot-pipeline/README.md`：观测键 / `GET /v1/cameras` / `robot.depth` / 现场核对步骤
-   [x] 计划登记 `wiki/plan/index.md`

## 验证记录

-   **离线端到端**（临时脚本 `/tmp/verify_depth_e2e.py`，不入库）：`TestRobot`（合成深度）→
    `capture_frames()` → `_ShmPublisher`（共享内存 v7，`depth_count=1`）→ `ObsShmReader` →
    `TestRobotAdapter.observe()`（`observations/depth/cam_head`）→ `FrameManager` →
    `DepthService.depth()`：`u = 0 / 0.5 / 1 → 1.0 / 1.32 / 1.639 m`（= `(1000 + u_px) / 1000`），
    无深度相机 → `DepthError`。
-   **待现场**：真机 RealSense 的 `depth_scale` / 彩色内参 / 对齐效果核对（README 已列步骤），
    以及三路同时开深度时的帧率影响。
