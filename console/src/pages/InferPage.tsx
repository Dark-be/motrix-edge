import { useCallback } from "react";

import { useEdge } from "../edge";
import { CameraRail } from "../layout/CameraRail";
import { PageLayout } from "../layout/PageLayout";
import { ObservationCard } from "../panels/ObservationCard";
import { SessionHooks } from "../panels/SessionHooks";
import { StatusStrip } from "../shell/StatusStrip";
import { TelemetryViewport } from "../viewport/TelemetryViewport";
import { SessionDeck } from "../components/SessionDeck";
import { useAdapterInfo } from "../hooks/useAdapterInfo";
import type { InferRolloutResponse } from "../types";

/**
 * 03 INFER —— 推理页。
 *
 * 左：推理卡片（策略配置 / 进入并预热 / 单步 / 持续 / 录制 + **推理状态并进同一张卡**）+ Hooks；
 * 中：3D（实测姿态 + 回归动作块的**期望轨迹**）；右：相机。
 */
export function InferPage() {
    const {
        api,
        hasLease,
        nodeState,
        sessionType,
        captures,
        infers,
        health,
        arms,
        onLog,
        setTrajectory,
        threeEnabled,
    } = useEdge();
    const { details } = useAdapterInfo();
    const inInfer = nodeState === "active" && sessionType === "infer";
    const rtc = infers?.rtc ?? null;
    const step = rtc?.index ?? null;

    /** 推理回执 → 期望轨迹：`actions` 是动作块，3D 视口按 layout 正解成末端折线。 */
    const onRollout = useCallback(
        (resp: InferRolloutResponse) => {
            const actions = (resp.actions ?? []).filter((row): row is number[] => Array.isArray(row));
            if (actions.length === 0) return;
            const layout = guessLayout(actions[0].length, arms.length || 1, details?.capabilities?.action_dims ?? {});
            setTrajectory({ actions, layout, step, source: "infer rollout", at: Date.now() });
        },
        [arms.length, details, setTrajectory, step]
    );

    const live = hasLease && inInfer;
    const warming = infers?.warming ?? false;
    const warmedUp = infers?.warmed_up ?? false;
    const occupiedByCapture = nodeState === "active" && sessionType === "capture";

    return (
        <>
            <StatusStrip
                mode="INFER"
                tone="green"
                live={{
                    ok: live,
                    label: live
                        ? warming
                            ? "预热中"
                            : "REAL ROBOT · LIVE"
                        : occupiedByCapture
                        ? "当前为采集会话"
                        : "准备推理",
                }}
                stage={`STEP ${step ?? "—"} · CHUNK ${rtc?.last_chunk?.height ?? "—"}`}
            />
            <PageLayout
                left={
                    <>
                        <SessionDeck
                            api={api}
                            hasLease={hasLease}
                            nodeState={nodeState}
                            sessionType={sessionType}
                            captures={captures}
                            infers={infers}
                            policies={health?.adapters.policies ?? []}
                            onLog={onLog}
                            fixedKind="infer"
                            onRollout={onRollout}
                            extra={<InferStatus infers={infers} />}
                        />
                        <SessionHooks />
                    </>
                }
                center={
                    <>
                        <div className={threeEnabled ? "min-h-[18rem] flex-1" : "shrink-0"}>
                            <TelemetryViewport mode={warmedUp || inInfer ? "INFER" : "IDLE"} />
                        </div>
                        <ObservationCard />
                    </>
                }
                right={<CameraRail />}
            />
        </>
    );
}

/** 推理状态（并进推理卡片）：连接 / 预热 / 运行位 + RTC 摘要与耗时。 */
function InferStatus({ infers }: { infers: ReturnType<typeof useEdge>["infers"] }) {
    const rtc = infers?.rtc ?? null;
    return (
        <div className="space-y-1 border-t border-slate-800 pt-2">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-slate-500">
                <span>
                    state <span className="font-mono text-slate-200">{infers?.state ?? "—"}</span>
                </span>
                <span>
                    policy <span className="font-mono text-slate-200">{infers?.policy ?? "—"}</span>
                </span>
                <span>
                    conn <span className="font-mono text-slate-200">{infers?.connected ? "on" : "off"}</span>
                </span>
                <span>
                    warm{" "}
                    <span className="font-mono text-slate-200">
                        {infers?.warming ? "…" : infers?.warmed_up ? "ok" : "no"}
                    </span>
                </span>
                <span>
                    rec{" "}
                    <span className="font-mono text-slate-200">{infers?.capture_status?.running ? "on" : "off"}</span>
                </span>
                <span>
                    cont <span className="font-mono text-slate-200">{infers?.continuous ? "on" : "off"}</span>
                </span>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-slate-500">
                <span>
                    RTC{" "}
                    <span className="font-mono text-slate-200">
                        {rtc?.enabled
                            ? `${rtc.params.prefix_len}/${rtc.params.execution_horizon ?? "auto"}/${
                                  rtc.params.suffix_len
                              }`
                            : "off"}
                    </span>
                </span>
                <span>
                    chunk <span className="font-mono text-slate-200">{rtc?.last_chunk?.height ?? "—"}</span>
                </span>
                <span>
                    delay{" "}
                    <span className="font-mono text-slate-200">
                        {rtc?.last_delay !== undefined ? `${(rtc.last_delay * 1000).toFixed(0)}ms` : "—"}
                    </span>
                </span>
                <span>
                    queue <span className="font-mono text-slate-200">{rtc?.remaining ?? "—"}</span>
                </span>
                <span>
                    drop <span className="font-mono text-slate-200">{infers?.dropped_actions ?? 0}</span>
                </span>
            </div>
        </div>
    );
}

/** 动作块 layout 判定：优先用 adapter 上报的各空间维度（`action_dims`），否则按**每臂长度**猜。 */
export function guessLayout(flat: number, armCount: number, dims: Record<string, number>): string {
    const per = armCount > 0 ? flat / armCount : flat;
    for (const [space, dim] of Object.entries(dims ?? {})) {
        if (dim !== flat) continue;
        if (space === "joint" && per === 7) return "joint+gripper";
        return space;
    }
    if (per === 7) return "joint+gripper";
    if (per === 6) return "joint";
    if (per === 12) return "pose";
    if (per === 1) return "gripper";
    return "joint";
}
