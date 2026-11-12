import type { EdgeApi } from "../api";
import type { InferRolloutResponse, InferStatus } from "../types";
import { SignalGrid, type Signal } from "./SignalGrid";
import { Badge, Disclosure, KeyValue, KvGrid } from "./ui";

/**
 * 推理会话内**特有**的操作与状态（录制 / 退出 / 策略配置由上层 `SessionDeck` 统一负责）。
 *
 * 只留三件「推理由谁驱动」的事：**预热**（连接 + prepare + 取一块丢弃，不下发动作）、
 * **单步**、**持续**；RTC 由上层渲染为同级卡片，避免 Infer 卡片内再嵌卡片。
 */
export function InferOps({
    api,
    hasLease,
    inInfer,
    busy,
    run,
    infers,
    onRollout,
}: {
    api: EdgeApi | null;
    hasLease: boolean;
    inInfer: boolean;
    busy: boolean;
    run: (label: string, fn: () => Promise<unknown>) => Promise<void>;
    infers: InferStatus | null;
    /** 推理回执回调（推理页把动作块喂给 3D 视口画期望轨迹） */
    onRollout?: (resp: InferRolloutResponse) => void;
}) {
    const connected = api !== null;
    const inferConnected = infers?.connected ?? false;
    const warming = infers?.warming ?? false;
    const warmedUp = infers?.warmed_up ?? false;
    const warmupError = infers?.warmup_error ?? null;
    const continuous = infers?.continuous ?? false;
    const droppedActions = infers?.dropped_actions ?? 0;
    // warmup_required 是公共配置项（缺省 true）：为真时「已预热」是 rollout 的硬前置
    const warmupRequired = (infers?.policy_config?.values?.warmup_required ?? true) !== false;
    const warmupReady = inferConnected && (!warmupRequired || warmedUp);

    const signals: Signal[] = [
        {
            // 预热可能几十秒～几分钟（加载模型）→ 后端立即回执，进度由 1s 轮询的状态驱动
            label: warming ? "预热中…" : warmupError ? "重试预热" : warmedUp ? "重新预热" : "连接并预热",
            command: "infer connect",
            endpoint: "POST /v1/infers/connect",
            variant: warmupReady ? "default" : "primary",
            enabled: connected && hasLease && inInfer && !warming,
            whyDisabled: inInfer ? "预热进行中（完成后再点）" : "需先进入推理会话",
            run: () => api!.inferConnect(),
        },
        {
            label: "推理一步",
            command: "infer rollout",
            endpoint: "POST /v1/infers/rollout",
            variant: "default",
            enabled: connected && hasLease && inInfer && warmupReady && !continuous,
            whyDisabled: !inInfer
                ? "需先进入推理会话"
                : !warmupReady
                ? "需先「连接并预热」（未预热时 rollout 会 409）"
                : "持续推理进行中（先停止）",
            run: async () => {
                const resp = await api!.inferRollout();
                onRollout?.(resp);
                return resp;
            },
        },
        {
            label: continuous ? "停止推理" : "持续推理",
            command: continuous ? "infer rollout stop" : "infer rollout continuous",
            endpoint: continuous ? "POST /v1/infers/rollout/stop" : "POST /v1/infers/rollout",
            variant: continuous ? "danger" : "primary",
            enabled: connected && hasLease && inInfer && (continuous || warmupReady),
            whyDisabled: !inInfer ? "需先进入推理会话" : "需先「连接并预热」（未预热时 rollout 会 409）",
            run: async () => {
                const resp = continuous ? await api!.stopInferRollout() : await api!.inferRollout("continuous");
                onRollout?.(resp as InferRolloutResponse);
                return resp;
            },
        },
    ];

    const metadata = infers?.metadata ?? null;

    return (
        <div className="space-y-2">
            <div>
                <SignalGrid signals={signals} busy={busy} onRun={(label, fn) => void run(label, fn)} />
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                    <Badge tone={inferConnected ? "green" : "slate"}>{inferConnected ? "已连接" : "未连接"}</Badge>
                    <Badge tone={warming ? "amber" : warmedUp ? "green" : warmupRequired ? "amber" : "slate"}>
                        {warming ? "预热中…" : warmedUp ? "已预热" : warmupRequired ? "未预热" : "免预热"}
                    </Badge>
                    {droppedActions > 0 && <Badge tone="amber">超时丢弃 {droppedActions} 步</Badge>}
                </div>
                {warmupError && <p className="mt-1 text-[11px] text-amber-300">上次预热未完成：{warmupError}</p>}
            </div>

            <Disclosure label="Info">
                <KvGrid>
                    <KeyValue k="Prompt（生效值）" v={infers?.prompt ?? "—"} />
                    <KeyValue k="录制 operator" v="policy" />
                    <KeyValue
                        k="录制 task_name"
                        v={infers?.prompt_required ? infers?.prompt ?? "—" : "（该策略不带）"}
                    />
                    <KeyValue k="策略类型" v={infers?.policy ?? "—"} />
                </KvGrid>
                {metadata ? (
                    <pre className="mt-2 max-h-40 overflow-auto rounded bg-slate-950/70 p-2 font-mono text-[10px] text-sky-200">
                        {JSON.stringify(metadata, null, 2)}
                    </pre>
                ) : (
                    <p className="mt-1 text-[10px] text-slate-500">无 metadata</p>
                )}
            </Disclosure>
        </div>
    );
}
