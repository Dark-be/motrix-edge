import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { EdgeApi } from "./api";
import { useLease } from "./hooks/useLease";
import { usePolling } from "./hooks/usePolling";
import { useToasts } from "./hooks/useToasts";
import { errMsg } from "./types";
import type {
    BoundAdapter,
    CaptureStatusSnapshot,
    CapturesStatus,
    HealthResponse,
    InferStatus,
    PreviewResponse,
    AuthMe,
    AuthStatus,
} from "./types";

const STORAGE_KEY = "edge-console:baseUrl";
const RATE_KEY = "edge-console:previewHz";
/** 节点状态轮询间隔（1 Hz）：health 含磁盘 / 适配器快照，够用即可。 */
export const POLL_MS = 1000;
/** 会话状态轮询间隔（5 Hz）：`capture_status`（含同步到机器人进程的 meta：时长 / 帧数）刷得快些。 */
export const SESSION_POLL_MS = 200;
/** 预览频率可选项（Hz）：3D 视口靠它刷新，1 Hz 够看状态，10 Hz 才跟得上运动。 */
export const PREVIEW_RATES = [1, 5, 10] as const;
const MAX_LOG = 80;

export interface LogEntry {
    ts: number;
    msg: string;
    ok: boolean;
}

/** 一次推理回报的**动作块**（期望轨迹的原始数据；FK 在 3D 视口里算）。 */
export interface ChunkTrajectory {
    /** 逐步动作向量（形状同一次 `infer rollout` 的 `actions`） */
    actions: number[][];
    /** 每步动作的语义：joint / pose / pose_delta / gripper（`+` 分隔的段序列） */
    layout: string;
    /** 采集时的 RTC 步号（视口底部标签用） */
    step: number | null;
    /** 来源说明（视口角标用，如 `infer rollout` / `manual target`） */
    source: string;
    at: number;
}

/** 手动页面暂存的目标（未下发）：3D 视口据此画目标臂 / 目标点，切页也不丢。 */
export interface ManualTarget {
    /** 动作空间（`joint` / `pose` / `pose_delta` / `gripper`） */
    space: string;
    /** 下发用的 layout（含夹爪时为 `joint+gripper` 这类段序列） */
    layout: string;
    /** 展平后的整条目标向量（按 `arms` 逐臂排布） */
    flat: number[];
}

/** 全局 Edge 会话状态（连接 / 轮询 / 租约 / 日志）——各页面共用一份，避免重复轮询。 */
export interface EdgeState {
    api: EdgeApi | null;
    baseUrl: string;
    connect: (url: string) => void;
    disconnect: () => void;
    connected: boolean;
    /** 连接时刻（顶部 `t+` 计时基准） */
    since: number;

    health: HealthResponse | null;
    healthError: string | null;
    captures: CapturesStatus | null;
    capturesError: string | null;
    infers: InferStatus | null;
    infersError: string | null;
    preview: PreviewResponse | null;
    previewError: string | null;

    lease: ReturnType<typeof useLease>;
    hasLease: boolean;
    authStatus: AuthStatus | null;
    auth: AuthMe | null;
    refreshAuth: () => Promise<void>;

    nodeState: string | null;
    sessionType: string | null;
    sessionState: string | null;
    adapter: BoundAdapter | null;
    captureStatus: CaptureStatusSnapshot | null;

    /** 相机名（预览观测里的 images） */
    cameraNames: string[];
    /** 启用臂名 */
    arms: string[];

    logs: LogEntry[];
    onLog: (msg: string, ok: boolean) => void;
    clearLogs: () => void;
    toasts: ReturnType<typeof useToasts>["toasts"];
    dismissToast: (id: number) => void;

    /** 期望轨迹（最近一次推理块 / 手动目标） */
    trajectory: ChunkTrajectory | null;
    setTrajectory: (t: ChunkTrajectory | null) => void;
    /** 手动页面暂存的目标（未下发；供 3D 视口画目标臂 / 目标点） */
    manualTarget: ManualTarget | null;
    setManualTarget: (t: ManualTarget | null) => void;
    /** 观测预览轮询频率（Hz）：3D 视口随它刷新，1 Hz 省流、10 Hz 跟手 */
    previewHz: number;
    setPreviewHz: (hz: number) => void;
    /** Three.js 视口开关；默认关闭，关闭时不挂载 WebGL renderer / 渲染循环。 */
    threeEnabled: boolean;
    setThreeEnabled: (enabled: boolean) => void;

    /** 受控操作统一出口：执行 + 记日志 + 弹 toast */
    run: (label: string, fn: () => Promise<unknown>) => Promise<void>;
    estop: () => void;
    resetNode: () => void;
}

const Ctx = createContext<EdgeState | null>(null);

export function useEdge(): EdgeState {
    const value = useContext(Ctx);
    if (!value) throw new Error("useEdge 必须在 <EdgeProvider> 内使用");
    return value;
}

export function EdgeProvider({ children }: { children: ReactNode }) {
    const [baseUrl, setBaseUrl] = useState<string>(() => localStorage.getItem(STORAGE_KEY) ?? "");
    const [api, setApi] = useState<EdgeApi | null>(null);
    const [since, setSince] = useState<number>(0);
    const [logs, setLogs] = useState<LogEntry[]>([]);
    const [trajectory, setTrajectory] = useState<ChunkTrajectory | null>(null);
    const [manualTarget, setManualTarget] = useState<ManualTarget | null>(null);
    const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
    const [auth, setAuth] = useState<AuthMe | null>(null);
    const [previewHz, setPreviewHzState] = useState<number>(() => {
        const saved = Number(localStorage.getItem(RATE_KEY));
        return (PREVIEW_RATES as readonly number[]).includes(saved) ? saved : 10;
    });
    // 有意不持久化：每次打开 Console 都默认关闭，避免无人值守页面占用 GPU。
    const [threeEnabled, setThreeEnabled] = useState(false);
    const { toasts, push, dismiss } = useToasts();

    const connected = api !== null;
    const lease = useLease(api);
    const hasLease = lease.holding;

    const refreshAuth = useCallback(async () => {
        if (!api) {
            setAuthStatus(null);
            setAuth(null);
            return;
        }
        try {
            const status = await api.authStatus();
            setAuthStatus(status);
            if (!status.enabled) {
                setAuth(null);
                return;
            }
            try {
                setAuth(await api.authMe());
            } catch {
                setAuth(null);
            }
        } catch {
            setAuthStatus(null);
            setAuth(null);
        }
    }, [api]);

    useEffect(() => {
        void refreshAuth();
    }, [refreshAuth]);

    const health = usePolling<HealthResponse>(() => api!.health(), POLL_MS, connected);
    const captures = usePolling<CapturesStatus>(() => api!.capturesStatus(), SESSION_POLL_MS, connected);
    const infers = usePolling<InferStatus>(() => api!.inferStatus(), SESSION_POLL_MS, connected);
    // 观测预览属受控操作（须持租约）：无租约不轮询，避免刷 409
    const preview = usePolling<PreviewResponse>(
        () =>
            api!.preview().catch((e) => {
                // 预览轮询最易撞上租约过期（410）：立即刷新租约 → 自动撤销 + 提示重新激活
                if (typeof e === "object" && e && "status" in e && (e as { status: number }).status === 410) {
                    void lease.refresh();
                }
                throw e;
            }),
        Math.max(50, Math.round(1000 / previewHz)),
        connected && hasLease
    );

    const setPreviewHz = useCallback((hz: number) => {
        setPreviewHzState(hz);
        localStorage.setItem(RATE_KEY, String(hz));
    }, []);

    // 记住的地址自动重连：刷新 / 重新打开页面不必再手点「连接」
    useEffect(() => {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (saved) {
            setApi(new EdgeApi(saved));
            setSince(Date.now());
        }
    }, []);

    const onLog = useCallback(
        (msg: string, ok: boolean) => {
            setLogs((prev) => [{ ts: Date.now(), msg, ok }, ...prev].slice(0, MAX_LOG));
            push(msg.length > 88 ? `${msg.slice(0, 88)}…` : msg, ok);
        },
        [push]
    );

    const clearLogs = useCallback(() => setLogs([]), []);

    const run = useCallback(
        async (label: string, fn: () => Promise<unknown>): Promise<void> => {
            try {
                const receipt = await fn();
                const text = receipt === undefined ? "" : JSON.stringify(receipt);
                onLog(`✓ ${label}${text && text !== "{}" ? ` → ${text}` : ""}`, true);
            } catch (e) {
                onLog(`✗ ${label} → ${errMsg(e)}`, false);
            }
        },
        [onLog]
    );

    const connect = useCallback((url: string) => {
        const trimmed = url.trim().replace(/\/+$/, "");
        if (!trimmed) return;
        setBaseUrl(trimmed);
        localStorage.setItem(STORAGE_KEY, trimmed);
        setApi(new EdgeApi(trimmed));
        setSince(Date.now());
    }, []);

    const disconnect = useCallback(() => {
        setApi(null);
        setAuthStatus(null);
        setAuth(null);
        setTrajectory(null);
        setManualTarget(null);
    }, []);

    const estop = useCallback(() => {
        void api?.command("robot/estop");
    }, [api]);

    const resetNode = useCallback(() => {
        void api?.command("node/reset");
    }, [api]);

    const arms = useMemo(() => preview.data?.observation.arms ?? [], [preview.data]);
    const cameraNames = useMemo(() => preview.data?.observation.images ?? [], [preview.data]);

    const value: EdgeState = {
        api,
        baseUrl,
        connect,
        disconnect,
        connected,
        since,
        health: health.data,
        healthError: health.error,
        captures: captures.data,
        capturesError: captures.error,
        infers: infers.data,
        infersError: infers.error,
        preview: preview.data,
        previewError: preview.error,
        lease,
        hasLease,
        authStatus,
        auth,
        refreshAuth,
        nodeState: captures.data?.node_state ?? null,
        sessionType: captures.data?.session_type ?? null,
        sessionState: captures.data?.state ?? null,
        adapter: captures.data?.adapter ?? null,
        captureStatus: captures.data?.capture_status ?? null,
        cameraNames,
        arms,
        logs,
        onLog,
        clearLogs,
        toasts,
        dismissToast: dismiss,
        trajectory,
        setTrajectory,
        manualTarget,
        setManualTarget,
        previewHz,
        setPreviewHz,
        threeEnabled,
        setThreeEnabled,
        run,
        estop,
        resetNode,
    };

    return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
