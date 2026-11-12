/** Edge HTTP API client：统一封装 fetch + X-Lease-Id / X-Correlation-Id 注入 + 错误解析。 */

import {
    ApiError,
    type AdapterConfig,
    type AdapterEffectiveConfig,
    type AdaptersResponse,
    type CaptureMetaResponse,
    type CapturesStatus,
    type CaptureSyncResponse,
    type CommandResponse,
    type DepthResponse,
    type EnterResponse,
    type HealthResponse,
    type InferConfigResponse,
    type InferConnectResponse,
    type InferEnterResponse,
    type InferEpisodeResponse,
    type InferRolloutResponse,
    type InferRolloutStopResponse,
    type InferRTCResponse,
    type InferStatus,
    type InferSyncResponse,
    type LeaseInstallRequest,
    type PrecheckResponse,
    type UploadPackResponse,
    type UploadStatus,
    type LeaseInstallResponse,
    type LeaseRenewResponse,
    type LeaseStatus,
    type PreviewResponse,
    type RpentEnvelope,
    type RpentInfoResponse,
    type WebRTCAnswerResponse,
    type AuthMe,
    type AuthStatus,
} from "./types";

let _cmdCounter = 0;
function newCommandId(): string {
    _cmdCounter += 1;
    return `cmd_${Date.now().toString(36)}_${_cmdCounter}`;
}

interface RequestOptions {
    body?: unknown;
    /** 受控操作：注入 X-Lease-Id 头（须持有活跃租约）。 */
    lease?: boolean;
    query?: Record<string, string>;
    headers?: Record<string, string>;
    /** 超时（毫秒，缺省 15s）：上传类长任务可调大。 */
    timeoutMs?: number;
    /** 覆盖请求 origin（认证 / Edge 注册表固定走当前 Console 同源）。 */
    baseUrl?: string;
}

export class EdgeApi {
    private _baseUrl: string;
    private _leaseId: string | null = null;
    private _authEnabled = false;

    constructor(baseUrl: string) {
        this._baseUrl = baseUrl.trim().replace(/\/+$/, "");
    }

    get baseUrl(): string {
        return this._baseUrl;
    }

    get leaseId(): string | null {
        return this._leaseId;
    }

    set leaseId(v: string | null) {
        this._leaseId = v;
    }

    private async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
        const headers: Record<string, string> = { ...(opts.headers ?? {}) };
        if (opts.body !== undefined) headers["Content-Type"] = "application/json";
        if (opts.lease && this._leaseId) headers["X-Lease-Id"] = this._leaseId;

        // 超时保护：Edge 端 handler 是同步阻塞的（命令 submit 最长 5s、上传扫描算 SHA-256），
        // 没有超时会让按钮永远 busy 且看不出是卡住还是没返回。上传类请求给更长的预算。
        const timeoutMs = opts.timeoutMs ?? 15000;
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), timeoutMs);
        const qs = opts.query ? `?${new URLSearchParams(opts.query).toString()}` : "";
        let res: Response;
        try {
            res = await fetch(`${opts.baseUrl ?? this._baseUrl}${path}${qs}`, {
                method,
                headers,
                credentials: this._authEnabled ? "include" : "omit",
                body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
                signal: controller.signal,
            });
        } catch (e) {
            if (controller.signal.aborted) throw new ApiError(0, `请求超时（${timeoutMs / 1000}s）：${method} ${path}`);
            throw e;
        } finally {
            window.clearTimeout(timer);
        }
        if (!res.ok) {
            let detail = res.statusText;
            try {
                const data: unknown = await res.json();
                if (data && typeof data === "object" && "detail" in data) {
                    const d = (data as { detail?: unknown }).detail;
                    if (typeof d === "string") detail = d;
                }
            } catch {
                /* 非 JSON 响应，用 statusText */
            }
            throw new ApiError(res.status, detail);
        }
        return (await res.json()) as T;
    }

    // ---- /v1/auth ------------------------------------------------------
    async authStatus(): Promise<AuthStatus> {
        const status = await this.request<AuthStatus>("GET", "/api/auth", { baseUrl: window.location.origin });
        this._authEnabled = status.enabled;
        return status;
    }

    authMe(): Promise<AuthMe> {
        return this.request("GET", "/api/auth/me", { baseUrl: window.location.origin });
    }

    authLoginUrl(returnTo = window.location.pathname): string {
        const query = new URLSearchParams({ return_to: returnTo.startsWith("/") ? returnTo : "/" });
        return `${window.location.origin}/api/auth/feishu/login?${query.toString()}`;
    }

    authLogout(): Promise<{ authenticated: false }> {
        return this.request("POST", "/api/auth/logout", { baseUrl: window.location.origin });
    }

    consoleEdges(): Promise<{ edges: Array<{ id: string; label: string }> }> {
        return this.request("GET", "/api/edges", { baseUrl: window.location.origin });
    }

    // ---- /v1/health / /v1/adapters -------------------------------------
    health(): Promise<HealthResponse> {
        return this.request("GET", "/v1/health");
    }

    adapters(): Promise<AdaptersResponse> {
        return this.request("GET", "/v1/adapters");
    }

    adapterConfig(): Promise<AdapterConfig> {
        // 运行时 adapter 能力配置（enabled_arms / enabled_cameras）
        return this.request("GET", "/v1/adapters/config");
    }

    adapterCurrent(): Promise<AdapterEffectiveConfig> {
        // 当前绑定 adapter 实际生效的能力配置（enabled_arms / enabled_cameras / action_dim / home）
        return this.request("GET", "/v1/adapters/current");
    }

    setAdapterConfig(config: Partial<AdapterConfig>): Promise<AdapterConfig> {
        // 设置运行时 adapter 能力配置（可部分更新；应用到当前已绑定 adapter；受控操作）
        return this.request("POST", "/v1/adapters/config", { lease: true, body: config });
    }

    // ---- /v1/uploads ---------------------------------------------------
    // 上传族全部为受控操作（Edge 侧 `_ensure_upload_lease`）→ 一律注入 X-Lease-Id，
    // 否则未持租约时会被拒（403 lease mismatch）。
    uploadStatus(): Promise<UploadStatus> {
        return this.request("GET", "/v1/uploads", { lease: true });
    }

    scanUploads(folderPath?: string): Promise<UploadStatus> {
        // 扫描要对整目录算 SHA-256，慢于普通状态请求 → 放宽超时
        return this.request("POST", "/v1/uploads", {
            lease: true,
            body: folderPath ? { folder_path: folderPath } : {},
            timeoutMs: 120000,
        });
    }

    selectUploads(episodeIds: string[]): Promise<UploadStatus> {
        return this.request("POST", "/v1/uploads/select", { lease: true, body: { episode_ids: episodeIds } });
    }

    enqueueUploads(): Promise<UploadStatus> {
        return this.request("POST", "/v1/uploads/upload", { lease: true });
    }

    retryUploads(): Promise<UploadStatus> {
        return this.request("POST", "/v1/uploads/retry", { lease: true });
    }

    packUploads(name: string): Promise<UploadPackResponse> {
        // 打包（移动）选中 episode 到 <扫描目录>/<包名>/；重名 → 409（改名后重试）
        return this.request("POST", "/v1/uploads/pack", { lease: true, body: { name }, timeoutMs: 120000 });
    }

    // ---- /v1/leases（本页签发 / 续约 / 撤销；GET 仅展示，不作为本页持有）----
    /** Edge 当前租约状态（**仅展示**；绝不写入 _leaseId —— 本页只持有自己签发的租约）。 */
    leaseStatus(): Promise<LeaseStatus> {
        return this.request("GET", "/v1/leases");
    }

    /** 本页签发租约：成功后 _leaseId = 本页持有的租约（仅本页内存态，刷新即失联）。 */
    async installLease(payload: LeaseInstallRequest): Promise<LeaseInstallResponse> {
        const res = await this.request<LeaseInstallResponse>("POST", "/v1/leases", { body: payload });
        this._leaseId = res.lease_id;
        return res;
    }

    /** 续约本页租约：lease_version+1 + 新 expires_at（本页决定过期时刻）。 */
    renewLease(leaseVersion: number, expiresAt: string): Promise<LeaseRenewResponse> {
        const id = this._leaseId;
        if (!id) return Promise.reject(new ApiError(409, "no active lease"));
        return this.request("POST", `/v1/leases/${encodeURIComponent(id)}:renew`, {
            body: { lease_version: leaseVersion, expires_at: expiresAt },
        });
    }

    /** 撤销**本页租约**：成功后本页不再持有（_leaseId 清空）。 */
    async revokeLease(): Promise<{ status: string }> {
        const id = this._leaseId;
        if (!id) return Promise.reject(new ApiError(409, "no active lease"));
        const res = await this.request<{ status: string }>("POST", `/v1/leases/${encodeURIComponent(id)}:revoke`);
        this._leaseId = null;
        return res;
    }

    /** 撤销指定租约（**不改本页持有**）：用于「撤销占用并接管」清理其它会话的占用租约。 */
    async revokeLeaseById(leaseId: string): Promise<{ status: string }> {
        return this.request<{ status: string }>("POST", `/v1/leases/${encodeURIComponent(leaseId)}:revoke`);
    }

    // ---- /v1/captures --------------------------------------------------
    capturesStatus(): Promise<CapturesStatus> {
        return this.request("GET", "/v1/captures");
    }

    capturesPrecheck(): Promise<PrecheckResponse> {
        // 只读预检（免租约）：节点 / 会话 / 机器人就绪 + 磁盘 + lease_id / leasable
        return this.request("GET", "/v1/captures/precheck");
    }

    enter(): Promise<EnterResponse> {
        // 单 adapter 包：无 adapter 选择，POST /v1/captures 空 body 即可
        return this.request("POST", "/v1/captures", { lease: true, body: {} });
    }

    exit(): Promise<{ status: string }> {
        // 退出经 query 提交租约（DELETE /v1/captures?lease_id=）
        return this.request("DELETE", "/v1/captures", {
            query: this._leaseId ? { lease_id: this._leaseId } : {},
        });
    }

    capturesSync(meta: Record<string, unknown>): Promise<CaptureSyncResponse> {
        // 同步采集元信息（采集员 / 任务名等）到机器人进程（进程保存一轮数据时附加）
        return this.request("POST", "/v1/captures/sync", { lease: true, body: { meta } });
    }

    captureMeta(): Promise<CaptureMetaResponse> {
        // 采集元信息选项（config/capture.yml 的 meta 段）：前端选择列表；读列表免租约
        return this.request("GET", "/v1/captures/meta");
    }

    // 采集元信息选项**管理**（配置级：与机器人进程 / 会话状态无关，未进会话也能维护）：
    // 写操作是受控操作（改的是设备上的配置文件）→ 须持租约；回执与 GET 同构（{meta: 全量}）
    captureMetaAdd(key: string, value: string): Promise<CaptureMetaResponse> {
        return this.request("POST", "/v1/captures/meta", { lease: true, body: { key, value } });
    }

    captureMetaEdit(key: string, oldValue: string, newValue: string): Promise<CaptureMetaResponse> {
        // 重命名选项（old → new）；分类 / 选项不存在或重复 → 400
        return this.request("PATCH", "/v1/captures/meta", {
            lease: true,
            body: { key, old: oldValue, new: newValue },
        });
    }

    captureMetaDelete(key: string, value: string): Promise<CaptureMetaResponse> {
        // 删除选项（分类清空则一并删除分类）；key / value 走 query（选项值可能含空格 / 中文）
        return this.request("DELETE", "/v1/captures/meta", { lease: true, query: { key, value } });
    }

    captureMetaDeleteKey(key: string): Promise<CaptureMetaResponse> {
        // 删除整个分类
        return this.request("DELETE", `/v1/captures/meta/${encodeURIComponent(key)}`, { lease: true });
    }

    // ---- /v1/infers --------------------------------------------------
    inferStatus(): Promise<InferStatus> {
        return this.request("GET", "/v1/infers");
    }

    enterInfer(policyType?: string, config?: Record<string, unknown>): Promise<InferEnterResponse> {
        // 无回合概念：POST /v1/infers；policyType 可选；config = 整份策略配置（含公共项
        // 推理端点 host / port——**会话级**：进入会话时固化，改需退出重进；以及策略自身配置项）。
        const body: Record<string, unknown> = {};
        if (policyType) body.policy_type = policyType;
        if (config && Object.keys(config).length > 0) body.config = config;
        return this.request("POST", "/v1/infers", { lease: true, body });
    }

    inferRollout(mode?: "single" | "continuous"): Promise<InferRolloutResponse> {
        // 推理闭环：单步（缺省，一次 观测→推理→动作）或 continuous 持续；多步 / drain 已取消。
        const body: Record<string, unknown> = {};
        if (mode === "continuous") body.mode = mode;
        return this.request("POST", "/v1/infers/rollout", { lease: true, body });
    }

    stopInferRollout(): Promise<InferRolloutStopResponse> {
        // 停止持续推理：回到会话 READY（**不退会话、不断策略连接**），仍可继续单步 / 再次持续
        return this.request("POST", "/v1/infers/rollout/stop", { lease: true });
    }

    inferRecordStart(): Promise<InferEpisodeResponse> {
        // 开始一轮推理 rollout 录制（capture episode start）：robot 开始录 mcap（含 action）
        return this.request("POST", "/v1/infers/episode/start", { lease: true });
    }

    inferRecordEnd(): Promise<InferEpisodeResponse> {
        // 结束一轮推理 rollout 录制（capture episode end）：robot 保存该 episode
        return this.request("POST", "/v1/infers/episode/end", { lease: true });
    }

    inferRecordCancel(): Promise<InferEpisodeResponse> {
        // 取消本轮推理 rollout 录制：停止并删除当前 episode 文件
        return this.request("POST", "/v1/infers/episode/cancel", { lease: true });
    }

    inferSyncMeta(meta: Record<string, unknown>): Promise<InferSyncResponse> {
        // 录制 rollout 时同步采集元信息（默认 operator=policy、task_name=prompt）
        return this.request("POST", "/v1/infers/sync", { lease: true, body: { meta } });
    }

    inferConnect(): Promise<InferConnectResponse> {
        // 单次尝试连接推理节点：infer connect（成功回执含服务端 metadata）
        return this.request("POST", "/v1/infers/connect", { lease: true });
    }

    setInferRtc(params: Record<string, unknown>): Promise<InferRTCResponse> {
        // 运行期设置 RTC（实时动作块）参数（可部分）：写内存态 policy.rtc + 应用到运行中 manager
        return this.request("POST", "/v1/infers/rtc", { lease: true, body: params });
    }

    setInferConfig(config: Record<string, unknown>): Promise<InferConfigResponse> {
        // 运行期设置策略配置项（infer config set）：按当前策略 schema 校验并写入 + 应用到策略客户端
        // （prompt 就是普通配置项——旧的 `POST /v1/infers/prompt` 快捷端点已删除）
        return this.request("POST", "/v1/infers/config", { lease: true, body: { config } });
    }

    exitInfer(): Promise<{ status: string }> {
        return this.request("DELETE", "/v1/infers", {
            query: this._leaseId ? { lease_id: this._leaseId } : {},
        });
    }

    // ---- /v1/preview（受控操作：须持有有效租约）--------------------------
    preview(): Promise<PreviewResponse> {
        return this.request("GET", "/v1/preview", { lease: true });
    }

    /** 查某个像素的深度（米）：`u` / `v` 是**归一化**坐标（相对源分辨率，0.5 = 画面中心）。
     *
     * 受控操作（须持活跃租约）；`depth_raw = 0` = 该像素无有效深度（`depth_m` 为 null）。
     */
    depth(camera: string, u: number, v: number): Promise<DepthResponse> {
        return this.request("GET", "/v1/depth", {
            lease: true,
            query: { camera, u: String(u), v: String(v) },
        });
    }

    // ---- RPent 兼容面（POST /call，免租约的只有 healthz / session.* / env.get_env_meta / get_camera_meta）--

    /** RPent 面的自省（`GET /v1/rpent`，免租约）：已实现方法清单 + 租约解析状态。 */
    rpentInfo(): Promise<RpentInfoResponse> {
        return this.request("GET", "/v1/rpent");
    }

    /** RPent 兼容 RPC：**永远 HTTP 200**，失败在信封里（`ok=false` + `error` / `kind`）——
     * 故这里不抛错，把信封交给调用方渲染（与 RPent 客户端同一契约）。
     *
     * 租约：RPent 面读的是 **Edge 级租约状态**（控制台签发租约即满足），不走 `X-Lease-Id` 头；
     * 写方法（`env.move_delta` / `rotate_delta` / `move_to` / `look_at` / `set_gripper` …）
     * 没有活跃租约 → `ok=false` + `kind=lease`。超时给长预算：`settle` 判到位可能等到超时。
     */
    rpentCall(method: string, kwargs: Record<string, unknown> = {}): Promise<RpentEnvelope> {
        return this.request("POST", "/call", { body: { method, kwargs }, timeoutMs: 180000 });
    }

    // ---- /v1/commands --------------------------------------------------
    command(capability: string, params?: Record<string, unknown>): Promise<CommandResponse> {
        return this.request("POST", "/v1/commands", {
            lease: true,
            body: {
                command_id: newCommandId(),
                lease_id: this._leaseId,
                capability,
                params: params ?? {},
            },
        });
    }

    // ---- 机器人控制命令（capability 的便捷封装；语义见 command/naming.py）----
    /** 直接下发一条动作（`robot execute <qpos> [layout]`；layout = joint / pose / pose_delta / gripper）。 */
    robotExecute(qpos: number[], layout?: string): Promise<CommandResponse> {
        return this.command("robot/execute", layout ? { qpos, layout } : { qpos });
    }

    /** 遥操作；`followSpace` 选择 joint/pose，`followAxes` 是 pose 跟随白名单。 */
    robotTeleop(
        enabled: boolean,
        mode?: string,
        followAxes?: string[],
        followSpace?: string
    ): Promise<CommandResponse> {
        return this.command("robot/teleop", {
            enabled,
            ...(mode ? { mode } : {}),
            ...(enabled && followSpace ? { follow_space: followSpace } : {}),
            ...(enabled && followAxes ? { follow_axes: followAxes } : {}),
        });
    }

    /** 示教遥操作（`robot teach <enabled>`，等于 teleop absolute）。 */
    robotTeach(enabled: boolean, followAxes?: string[], followSpace?: string): Promise<CommandResponse> {
        return this.command("robot/teach", {
            enabled,
            ...(enabled && followSpace ? { follow_space: followSpace } : {}),
            ...(enabled && followAxes ? { follow_axes: followAxes } : {}),
        });
    }

    /** 人工接管（`robot takeover <enabled>`，等于 teleop delta：锚点增量，接管瞬间不突变）。 */
    robotTakeover(enabled: boolean, followAxes?: string[], followSpace?: string): Promise<CommandResponse> {
        return this.command("robot/takeover", {
            enabled,
            ...(enabled && followSpace ? { follow_space: followSpace } : {}),
            ...(enabled && followAxes ? { follow_axes: followAxes } : {}),
        });
    }

    /** 机器人复位到 home（`robot reset`）。 */
    robotReset(): Promise<CommandResponse> {
        return this.command("robot/reset");
    }

    /** 节点复位（`node reset`：ERROR → IDLE，push 型即发即忘）。 */
    nodeReset(): Promise<CommandResponse> {
        return this.command("node/reset");
    }

    // ---- /v1/webrtc/offer（受控操作：须持有有效租约）-----------------------
    webrtcOffer(sdp: string, type: string): Promise<WebRTCAnswerResponse> {
        return this.request("POST", "/v1/webrtc/offer", { lease: true, body: { sdp, type } });
    }
}

// HMR 守卫：EdgeApi 是**长生命周期实例**（App 只 new 一次），api.ts 类定义热更时旧实例
// 不会被替换 → 新方法在浏览器里「is not a function」。这里在热更后强制整页刷新，
// 保证实例与类定义一致（api.ts 改动本身很少，刷新代价可忽略）。
if (import.meta.hot) {
    import.meta.hot.accept(() => {
        window.location.reload();
    });
}
