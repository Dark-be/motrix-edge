/** Edge HTTP API 契约类型（与 src/motrix_edge/server 一致）。 */

// ==== /v1/health ====
export interface AdapterRef {
    /** 展示名称（discover 赋予 name） */
    name: string;
    /** adapter 类型（entry point 名，用于实例化） */
    type: string;
}

/** 策略配置项 schema（`policy.POLICY_CONFIG_ITEMS`；每个策略有自己的独立配置项）。
 *
 * 前端据此**动态渲染表单**：openpi → 文本指令 prompt；lerobot-act → 模型路径 / 设备 / 块长。
 */
export interface PolicyConfigItem {
    /** 配置键（写入 base_cfg.policy[key]；对应 POST /v1/infers/config 的 body 键） */
    key: string;
    /** 展示名（表单标签） */
    label: string;
    /** 控件 / 校验类型：text | int | float | bool */
    type: "text" | "int" | "float" | "bool";
    /** 是否必填（缺失时 `missing` 会列出该项；推理 / 录制按钮据此门控） */
    required?: boolean;
    /** 会话内改动是否**立即生效**：true = 策略每次请求现读；false = 会话级 / 握手级（退出重进才生效） */
    runtime?: boolean;
    /** 分组："endpoint" = 推理端点项（host / port，策略都连 TCP 端点）。前端按此分组
     *  门控「进入推理」，不硬编码键名 */
    group?: string;
    /** 长文本项（prompt / system_prompt）：前端渲染为多行 textarea（占满整行） */
    multiline?: boolean;
    /** multiline 的行数（缺省 6） */
    rows?: number;
    /** 数值项下界 / 上界（`type` 为 int / float 时用于表单校验） */
    min?: number;
    max?: number;
    /** 敏感项（密钥）：后端不回显值（`value` 恒空 + `configured` 标记），前端渲染密码框 */
    secret?: boolean;
    /** 代码缺省值（可选） */
    default?: string | number | boolean | null;
    /** 固定候选值；存在时渲染下拉框，提交值仍按 `type` 转换。 */
    options?: Array<string | number | boolean>;
    placeholder?: string;
    help?: string;
}

/** schema 项 + 当前值（GET /v1/infers 的 policy_config.items）。 */
export interface PolicyConfigItemValue extends PolicyConfigItem {
    value: string | number | boolean | null;
    /** 敏感项专用：后端内存态是否已设置（值不回显） */
    configured?: boolean;
}

/** 策略配置项状态（GET /v1/infers 的 policy_config；进入会话前后均可取）。 */
export interface PolicyConfigStatus {
    /** 当前策略类型（决定 items 的内容） */
    policy_type: string;
    /** 配置项清单（schema + 当前值） */
    items: PolicyConfigItemValue[];
    /** 当前值字典（key → value） */
    values: Record<string, string | number | boolean | null>;
    /** 缺失的必填项键（非空 → 不能开始推理 / 录制） */
    missing: string[];
    /** 会话内可运行时修改的键 */
    runtime_keys?: string[];
    /** 是否需要 prompt（语言条件策略，如 openpi） */
    requires_prompt: boolean;
    /** 是否需要模型路径（lerobot 类策略，如 lerobot-act） */
    requires_model_path: boolean;
}

export interface PolicyRef {
    type: string;
    class: string;
    module: string;
    /** 该策略的配置项 schema（选择策略后即可渲染表单，无需进入会话） */
    config_items?: PolicyConfigItem[];
}

export interface DiskInfo {
    total?: number;
    used?: number;
    free?: number;
    error?: string;
}

export interface HealthResponse {
    status: string;
    version: string;
    identity: Record<string, string>;
    robot: { name: string | null; type: string | null };
    adapters: { robots: AdapterRef[]; policies: PolicyRef[] };
    disk: DiskInfo;
    time: string;
}

// ==== /v1/adapters ====
/** 动作空间（值语义的区分）：joint = 关节角 / pose = 末端位姿（绝对）/
 * pose_delta = 末端位姿**增量**（机器人侧叠加在关节段目标上）/ gripper = 夹爪 */
export const ActionSpace = {
    JOINT: "joint",
    POSE: "pose",
    POSE_DELTA: "pose_delta",
    GRIPPER: "gripper",
} as const;
export type ActionSpace = (typeof ActionSpace)[keyof typeof ActionSpace];

export interface AdapterCapabilities {
    robot_model_id: string;
    robot_model_version: string;
    action_dim: number;
    /** 各动作空间的值维度（joint / pose / pose_delta / gripper → 维数） */
    action_dims: Record<string, number>;
    /** 支持的动作空间（joint / pose / pose_delta / gripper）：Console 据此开放对应直控 */
    action_spaces: string[];
    observation_keys: string[];
    image_names: string[];
    capabilities: Record<string, boolean>;
}

export interface BoundAdapter {
    /** 展示名称（discover 赋予 name） */
    name: string | null;
    /** adapter 类型（entry point 名，用于实例化） */
    type: string | null;
    running: boolean | null;
    /** 名义控制频率（robot env 主循环 HZ，Hz；node 周期缓存 adapter.health） */
    control_hz?: number | null;
    /** 实测主循环帧率（robot 最近窗口统计，Hz；node 周期缓存 adapter.health） */
    measured_hz?: number | null /** 遥操作（人工接管）是否开启：开启期间推理 rollout 会被进程拒绝 */;
    teleop?: boolean;
    /** 遥操作模式：`absolute`（示教）/ `delta`（增量人工接管）；未开启为 null */
    teleop_mode?: string | null;
    /** robot-pipeline 当前遥操作跟随空间；只有 pose 支持运行时锁轴 */
    teleop_follow_space?: "joint" | "pose" | string | null;
    /** pose teleop 当前/缺省跟随轴；未列轴即锁定 */
    teleop_follow_axes?: string[] | null;
}

export interface AdapterDetails {
    /** adapter 类型（entry point 名，类确定） */
    type: string;
    available: boolean;
    capabilities: AdapterCapabilities | null;
}

export interface AdaptersResponse {
    adapters: AdapterDetails[];
}

// ==== /v1/adapters/config（运行时 adapter 能力配置）====
export interface AdapterConfig {
    enabled_arms?: string[] | null;
    enabled_cameras?: string[] | null;
}

// ==== /v1/adapters/current（当前 adapter 实际生效能力）====
export interface AdapterEffectiveConfig {
    adapter: { name: string | null; type: string | null };
    /** 能力启用字典（前端勾选展示 / 同步）：arms 臂 → bool，cameras 相机 → bool */
    enabled: {
        arms: Record<string, boolean>;
        cameras: Record<string, boolean>;
    };
    action_dim: number | null;
    /** 各动作空间的**启用臂**值维度（joint / pose / gripper → 维数） */
    action_dims: Record<string, number>;
    /** 各动作空间的**全臂 home**（未启用臂填充用；与 action_dims 的键一一对应） */
    home: Record<string, number[]>;
}

// ==== /v1/uploads ====
export interface UploadFileInfo {
    path: string;
    size: number;
    modified_at: string;
    sha256: string;
}

export interface UploadEpisode {
    episode_id: string;
    status: string;
    mcap: UploadFileInfo | null;
    metadata: UploadFileInfo | null;
    /** JSON 描述文件的结构化字段（schema 提取；缺省/类型不符为 null） */
    meta: Record<string, string | number | null>;
    metadata_content: Record<string, unknown> | null;
    /** schema 未识别的原始字段（向前兼容新数据） */
    metadata_unknown: Record<string, unknown> | null;
    errors: string[];
    selected: boolean;
}

export interface UploadStatus {
    folder_path: string | null;
    scanned_at: string | null;
    endpoint_configured: boolean;
    episode_count: number;
    ready_count: number;
    invalid_count: number;
    selected_episode_ids: string[];
    /** 建议包名 ``pack<选中数量>``（未扫描 / 未选择 → 空串）：前端包名输入框预填值 */
    suggested_pack_name?: string;
    episodes: UploadEpisode[];
}

/** POST /v1/uploads/pack：打包（**移动**）选中 episode 到 ``<目录>/<包名>/`` 的回执。 */
export interface UploadPackResponse {
    /** 实际使用的包名（缺省 ``pack<选中数量>``） */
    name: string;
    /** 包目录绝对路径 */
    path: string;
    episode_count: number;
    episode_ids: string[];
    file_count: number;
    /** 移入包目录的文件绝对路径（.mcap + .json） */
    files: string[];
    /** 打包后的重扫状态（前端直接替换列表） */
    scan: UploadStatus;
}

// ==== /v1/leases（Console 权威：签发 / 续约 / 撤销；Edge 只保留 + 校验）====
export type LeaseState = "reserved" | "active" | "revoked" | "expired";

export interface LeaseStatus {
    lease_id: string | null;
    edge_id: string | null;
    holder_subject_id: string | null;
    purpose: string | null;
    state: LeaseState | null;
    expires_at: string | null;
    renewed_at: string | null;
    lease_version: number | null;
    ttl: number | null;
    renew_interval: number;
    default_ttl: number;
    leasable: boolean;
}

export interface LeaseInstallRequest {
    /** 租约唯一标识（本页生成） */
    lease_id: string;
    /** 租约所属 edge 设备 */
    edge_id: string;
    /** 租约所属操作员 */
    holder_subject_id: string;
    /** 租约用途（capture / rollout / maintenance） */
    purpose: string;
    /** 签发状态（reserved / active） */
    state: "reserved" | "active";
    /** 租约版本（首签 = 1） */
    lease_version: number;
    /** 过期时间（ISO 8601）—— 由本页（Console 侧）决定并随镜像下发 */
    expires_at: string;
    /** 有效期（秒，信息字段） */
    ttl?: number;
}

/** POST /v1/leases/{id}:renew 请求体：本页（Console 侧）决定新 expires_at。 */
export interface LeaseRenewRequest {
    /** 新租约版本（须高于当前，版本回退拒绝） */
    lease_version: number;
    /** 续约后的过期时间（ISO 8601）—— 由本页决定 */
    expires_at: string;
}

/** POST /v1/leases 响应 = {status} + 当前租约状态汇总（Edge 保存镜像后回显） */
export interface LeaseInstallResponse {
    status: string;
    lease_id: string | null;
    edge_id: string | null;
    holder_subject_id: string | null;
    purpose: string | null;
    state: LeaseState | null;
    expires_at: string | null;
    renewed_at: string | null;
    lease_version: number | null;
    ttl: number | null;
    renew_interval: number;
    default_ttl: number;
    leasable: boolean;
}

export interface LeaseRenewResponse {
    status: string;
    lease_id: string;
    lease_version: number;
    state: LeaseState;
    expires_at: string;
}

// ==== /v1/auth（飞书登录；Cookie 由浏览器管理）====
export interface AuthStatus {
    enabled: boolean;
    provider: "feishu" | null;
}

export interface AuthUser {
    provider: "feishu";
    subject: string;
    tenant_key: string;
    open_id: string;
    display_name: string;
    union_id?: string;
    user_id?: string;
}

export interface AuthMe {
    authenticated: true;
    user: AuthUser;
    authenticated_at: string;
}

// ==== /v1/captures ====
/** 采集状态缓存（`GET /v1/captures` / `GET /v1/infers` 的 `capture_status`）。
 *
 * 来自机器人进程：运行位 + `capture sync` 同步的**元信息全集**（分类可拓展，如
 * `operator` / `task_name`；不设同义顶层字段）。未绑定 / 未缓存 → null。
 */
export interface CaptureStatusSnapshot {
    running: boolean;
    /** 元信息全集（分类 → 值）；值可能是标量（`capture sync` 同步的 `operator` / `task_name`…），
     * 也可能是机器人进程的**结构化自描述**（`state_space` / `state_dims` / `action_space` /
     * `action_dims`；后两者是逐维 `{index, arm, kind, name}` 数组）→ 展示走 `fmtMetaValue`。 */
    meta: Record<string, unknown>;
    /** 数据目录（进程自维护；仅采集状态携带，推理状态无此字段） */
    data_dir?: string | null;
}

export interface CapturesStatus {
    node_state: "init" | "idle" | "ready" | "active" | "error" | null;
    /** 当前会话类型（capture / infer / null）：区分采集与推理会话 */
    session_type: string | null;
    state: string | null;
    /** 当前 discover 绑定的唯一 adapter（单 adapter 包；未绑定为 null） */
    adapter: BoundAdapter | null;
    /** 采集状态缓存（运行位 + 元信息全集 + 数据目录）：采集职责的**唯一来源** */
    capture_status: CaptureStatusSnapshot | null;
    disk: DiskInfo;
    lease_id: string | null;
}

export interface CaptureSyncResponse {
    status: string;
    meta: Record<string, unknown> | null;
}

export interface CaptureMetaResponse {
    /** 采集元信息选项：分类 → 选项数组（config/capture.yml，capture meta 命令族 / HTTP 维护）
     *
     * `GET /v1/captures/meta` 读列表（免租约）；`POST` / `PATCH` / `DELETE` 的写回执**同构**
     * （`{meta: 全量}`，须租约）——前端写后无需再拉取。
     */
    meta: Record<string, string[]>;
}

export interface EnterResponse {
    status: string;
    state: string;
    lease_id: string;
    adapter: AdapterRef;
}

// ==== /v1/infers ====
export interface InferStatus {
    node_state: "idle" | "ready" | "active" | "error" | null;
    /** 当前会话类型（capture / infer / null）：区分采集与推理会话 */
    session_type: string | null;
    state: string | null;
    adapter: BoundAdapter | null;
    policy: string | null;
    /** 策略服务器连接状态（infer connect 成功后为 true） */
    connected: boolean;
    /** 连接成功后的服务端 metadata（未连接 / 无会话为 null） */
    metadata: Record<string, unknown> | null;
    /** 当前推理文本指令（**仅需要 prompt 的策略**：推理 / 录制前必须非空） */
    prompt: string | null;
    /** 该策略是否需要 prompt（openpi=true 门控；lerobot-act=false 不参与门控） */
    prompt_required: boolean;
    /** 是否已预热（infer connect：连接 + prepare + 取一块丢弃，**不下发动作**）：
     *  warmup_required=true（缺省）时它是 rollout 的前置（未预热 rollout 会被后端 409） */
    warmed_up: boolean;
    /** 预热工作线程是否进行中（异步：加载模型可能几分钟，期间命令照旧可响应） */
    warming: boolean;
    /** 上次预热未完成的原因（prepare / 取块失败 / 被急停取消；成功后清空） */
    warmup_error: string | null;
    /** 因回执超时被丢弃的动作数（未下发到真机；调用方超时时看这里定位） */
    dropped_actions: number;
    /** 推理会话当前是否开启 rollout 录制（capture episode start 后为 True） */
    recording: boolean;
    /** 持续推理是否运行中（`infer rollout continuous` ↔ `infer rollout stop`） */
    continuous: boolean;
    /** 机器人进程实际采集状态缓存（录制中 running=True + 已同步的元信息全集） */
    capture_status: CaptureStatusSnapshot | null;
    /** RTC（实时动作块）运行状态（enabled / params / index / remaining / last_chunk） */
    rtc: RtcStatus | null;
    /** 策略配置项状态（schema + 当前值 + 缺失必填项；前端据此渲染表单并门控按钮） */
    policy_config: PolicyConfigStatus | null;
    lease_id: string | null;
}

export interface InferEnterResponse {
    status: string;
    lease_id: string;
    adapter: AdapterRef;
    policy: string | null;
    /** 生效后的配置项状态 */
    policy_config?: PolicyConfigStatus | null;
}

export interface InferConnectResponse {
    status: string;
    state: string | null;
    /** 本次是否新启动了预热（false = 已在预热中 / 已预热完，幂等回执） */
    started: boolean;
    connected: boolean;
    /** 预热进行中（**立即回执**，不等跑完；调用方轮询 status 等 warmed_up） */
    warming: boolean;
    /** 预热是否已完成（连接 + prepare + 取一块丢弃） */
    warmed_up: boolean;
    /** 预热未完成时的原因（prepare / 取块失败 / 被取消；可重试本命令） */
    warmup_error: string | null;
    /** 预热那一块实测出的块长（供 RTC 校准；未取到为 null） */
    chunk_len: number | null;
    metadata: Record<string, unknown> | null;
}

export interface InferRolloutResponse {
    status: string;
    state: string | null;
    count: number | null;
    action: number[] | null;
    actions: (number[] | null)[] | null;
}

/** POST /v1/infers/rollout/stop：停止持续推理（退回会话 READY）。 */
export interface InferRolloutStopResponse {
    status: string;
    state: string | null;
    continuous: boolean;
}

/** POST /v1/infers/episode/start|end|cancel：推理时 rollout 录制开关。 */
export interface InferEpisodeResponse {
    status: string;
    episode: "start" | "end" | "cancel" | null;
    recording: boolean;
}

/** POST /v1/infers/sync：录制 rollout 时同步的采集元信息。 */
export interface InferSyncResponse {
    status: string;
    meta: Record<string, unknown> | null;
}

/** RTC（实时动作块）参数（`policy.rtc` 段；POST /v1/infers/rtc 可部分更新）。 */
export interface RtcParams {
    enabled: boolean;
    /** 块长上限 H（一次推理只取策略块的前 H 步）；模型块更短时 P/E/S 与提前量按「实际块长 / H」等比缩放 */
    action_horizon: number;
    /** 前置段 P（推理期间已被执行的前 P 步，跳过；也是提前发请求的提前量） */
    prefix_len: number;
    /** 执行段 E（null = 实际块长 - P - S；与上一块后缀重叠步按过渡策略加权 + 未重叠部分直接执行） */
    execution_horizon: number | null;
    /** 后缀段 S（与下一块的重叠窗口；0 = 关闭过渡） */
    suffix_len: number;
    /** 重叠过渡策略：weighted_average/conservative/average/latest_only/continuous */
    aggregate_fn: string;
}

/** RTC 状态（GET /v1/infers 的 rtc 字段）。
 *
 * **无推理会话时只有 `enabled` / `params`**（配置级 `policy.rtc` 快照，供未进入会话时展示）；
 * `index` / `remaining` / `fetches` 等运行字段只在会话内存在。 */
export interface RtcStatus {
    enabled: boolean;
    params: RtcParams;
    /** 下一个待下发步号（单调；仅会话内） */
    index?: number;
    /** 队列中从 index 起连续未消费步数（仅会话内） */
    remaining?: number;
    /** 会话内拉块次数（仅会话内） */
    fetches?: number;
    /** 最近一块的切分（prefix / execution / suffix 步数 + 块长 + 参与过渡的重叠步数；块长 = min(H, 模型块长)） */
    last_chunk: {
        start_index: number;
        height: number;
        lens: { prefix: number; execution: number; suffix: number };
        overlap_steps?: number;
    } | null;
    /** 最近一次推理耗时（秒；供定前置段 P 参考） */
    last_delay?: number;
    /** 实测推理耗时折算的控制步数（P 建议值） */
    last_delay_steps?: number;
}

/** POST /v1/infers/rtc：设置 RTC 参数，回执生效后的运行状态。 */
export interface InferRTCResponse {
    status: string;
    rtc: RtcStatus | null;
}

/** POST /v1/infers/config：设置策略配置项（按当前策略 schema 校验），回执生效后的状态。 */
export interface InferConfigResponse {
    status: string;
    policy_config: PolicyConfigStatus | null;
}

// ==== /v1/preview ====
export interface PreviewResponse {
    state: string;
    adapter: { id: string | null; name: string | null; type: string | null };
    observation: {
        qpos: number[] | null;
        /** 展示层从 7 维 qpos 拆出的 6 维关节段。 */
        gripper: number[] | null;
        /** Robot 显式提供的目标误差：每臂 `[joint_error6, gripper_error1]`。 */
        qpos_err: number[] | null;
        /** Robot 当前目标：每臂 `[joint6, gripper1]`，用于 3D 蓝色透明目标臂。 */
        action: number[] | null;
        /** **实测末端位姿 EEF**：每臂 6 维（xyz + rpy），物理顺序同 `arms`；机器人不提供位姿时为 null。 */
        pose: number[] | null;
        /** **目标位姿**（= FK(关节段目标)）：与 `pose` 同布局；「目标 − 实测」即 MIT 稳态误差。 */
        pose_target: number[] | null;
        /** 摄像头名列表（图像不内联，由 WebRTC /v1/webrtc/offer 逐相机推流）。 */
        images: string[];
        /** **有深度图**的相机名（机器人开了深度才有；像素深度查询见 `GET /v1/depth`）。 */
        depth: string[];
        /** 启用臂名（adapter 运行时配置）：前端据此给 `pose` / `qpos` 分行标注。 */
        arms: string[];
    };
}

// ==== /v1/depth ====
/** 像素深度查询结果：`u` / `v` 是**归一化**坐标，回执回显换算后的源像素 `u_px` / `v_px`。 */
export interface DepthResponse {
    camera: string;
    u: number;
    v: number;
    u_px: number;
    v_px: number;
    width: number;
    height: number;
    /** 深度原始值（uint16，已对齐到彩色图）；`0` = 该像素测不到。 */
    depth_raw: number;
    /** 米数（原始值 × `depth_scale`）；`depth_raw = 0` 或元数据缺失 → null。 */
    depth_m: number | null;
    valid: boolean;
    /** 深度原始值 → 米的比例（机器人上报）。 */
    depth_scale: number | null;
    /** **彩色内参**（对齐后深度与彩图共用像素网格）：反投影 `x = (u_px − cx)·z/fx` 用。 */
    intrinsics: { fx?: number; fy?: number; cx?: number; cy?: number };
    /**
     * 该像素在**相机光学系**下的 `[x, y, z]`（米）；无有效深度 / 缺内参 / 未标定 →
     * `null`（不是 `0`，调用方不必猜）。
     */
    xyz_camera: [number, number, number] | null;
    /**
     * 同一点在 `world` 下的 `[x, y, z]`（米）：Edge 用标定外参（腕相机还要**同一拍**的
     * 臂位姿）算好；未标定 / 该相机不在产物里 / 缺同拍位姿 → `null`。
     */
    xyz_world: [number, number, number] | null;
    /** `xyz_world` 的帧名（如 `"world"`）；不可用 → `null`。 */
    frame: string | null;
    /** `world` 帧的物理别名（如 `"left_base"` = 左臂基座）；不可用 → `null`。 */
    world: string | null;
}

// ==== /v1/commands ====
/** 命令回执状态：`ok` 执行成功 / `rejected` 业务拒绝（参数非法、状态不符）/ `error` /
 * `accepted`（push 型命令已入总线，无回执；estop 与 node reset）。 */
export type CommandStatus = "ok" | "rejected" | "error" | "accepted";

/** `GET /v1/rpent`：RPent 面的自省（已实现方法 + 租约解析状态；免租约）。 */
export interface RpentInfoResponse {
    enabled: boolean;
    /** RPC 端点（通常 `/call`）。 */
    endpoint: string;
    methods: string[];
    /** Edge 级租约状态（写方法是否需要先签发租约看这里）。 */
    lease: Record<string, unknown>;
}

/** `POST /call` 的回执信封：**失败也是 HTTP 200**，`ok=false` 时才看 `error` / `kind`。 */
export interface RpentEnvelope {
    ok: boolean;
    /** `ok=true` 时的返回值（方法自定形状）。 */
    result?: unknown;
    /** `ok=false` 时的可读原因。 */
    error?: string;
    /** 失败分类：`argument` / `state` / `unsupported` / `unavailable` / `lease` / `internal` … */
    kind?: string;
}

export interface CommandResponse {
    command_id: string;
    status: CommandStatus;
    idempotency_key: string | null;
    correlation_id: string;
    /** 执行的 capability（规范拼写，如 `robot/execute`；CommandService 透传；push 型命令为 null） */
    executed?: string | null;
    /** 执行错误（如 robot/execute 维度不符 / robot/teleop 参数非法） */
    error?: string | null;
    /** 执行结果数据：robot/execute → action；robot/teleop → teleop / mode；
     * capture/episode/start|end → episode / recording；capture/sync → meta */
    data?: Record<string, unknown> | null;
    /** 请求用了旧 capability 拼写（robot_execute 一类）→ true，调用方据此迁移 */
    deprecated?: boolean;
}

/** 命令是否成功（`ok`）；`accepted` 视为已下发（push 型无回执，不作失败）。 */
export function commandOk(res: CommandResponse | null | undefined): boolean {
    return res?.status === "ok" || res?.status === "accepted";
}

/** 命令回执 → 失败原因（成功 / accepted → null）。
 *
 * `/v1/commands` 的业务拒绝是 **HTTP 200 + status=rejected + error**（不是 HTTP 错误），
 * 只看 HTTP 会把它当成成功——所有命令类调用都用 `requireOk` 包一层。
 */
export function commandError(res: CommandResponse | null | undefined): string | null {
    if (!res || commandOk(res)) return null;
    return res.error ?? `命令被拒绝（${res.status}）`;
}

/** 命令调用包装：回执非 ok / accepted 时抛错（让调用方的统一失败分支接管）。 */
export async function requireOk<T extends CommandResponse>(promise: Promise<T>): Promise<T> {
    const res = await promise;
    const err = commandError(res);
    if (err) throw new Error(err);
    return res;
}

// ==== /v1/captures/precheck ====
/** 预检（只读，免租约）：进入采集前的就绪检查（节点 / 会话 / 机器人 / 磁盘 / 可否租）。 */
export interface PrecheckResponse {
    ok: boolean;
    node_state: string | null;
    state: string | null;
    robot_ready: boolean;
    disk: DiskInfo;
    errors: string[];
    lease_id: string | null;
    leasable: boolean;
}

// ==== /v1/webrtc/offer ====
export interface WebRTCAnswerResponse {
    sdp: string;
    type: string;
}

// ==== API 错误 ====
export class ApiError extends Error {
    status: number;
    detail: string;

    constructor(status: number, detail: string) {
        super(detail);
        this.name = "ApiError";
        this.status = status;
        this.detail = detail;
    }
}

export function errMsg(e: unknown): string {
    if (e instanceof ApiError) return `HTTP ${e.status}: ${e.detail}`;
    if (e instanceof Error) return e.message;
    return String(e);
}

/** 已同步 meta 的展示项：过滤掉空值（None / 空串 / 空数组 / 空对象）。
 *
 * meta 分类可能很多，全量铺开会把面板撑得很长；空值没有信息量，直接不显示。
 */
export function metaEntries<V>(meta: Record<string, V> | null | undefined): [string, V][] {
    const isEmpty = (v: unknown): boolean =>
        v === null ||
        v === undefined ||
        v === "" ||
        (Array.isArray(v) && v.length === 0) ||
        (typeof v === "object" && !Array.isArray(v) && Object.keys(v as object).length === 0);
    return (Object.entries(meta ?? {}) as [string, V][]).filter(([, value]) => !isEmpty(value));
}
