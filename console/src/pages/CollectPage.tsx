import { useEdge } from "../edge";
import { CameraRail } from "../layout/CameraRail";
import { PageLayout } from "../layout/PageLayout";
import { ObservationCard } from "../panels/ObservationCard";
import { MetaCard } from "../panels/MetaCard";
import { SessionHooks } from "../panels/SessionHooks";
import { StatusStrip } from "../shell/StatusStrip";
import { TelemetryViewport } from "../viewport/TelemetryViewport";
import { SessionDeck } from "../components/SessionDeck";

/**
 * 02 COLLECT —— 数采页。
 *
 * 左：Meta（登录 / 任务 / 同步）→ 采集卡片（进入会话 / 轮次起止）→ Hooks；中：3D；右：相机。
 */
export function CollectPage() {
    const { api, hasLease, nodeState, sessionType, captures, infers, health, onLog, threeEnabled } = useEdge();
    const inCapture = nodeState === "active" && sessionType === "capture";
    const recording = captures?.capture_status?.running ?? false;

    const live = hasLease && inCapture;
    const occupiedByInfer = nodeState === "active" && sessionType === "infer";

    return (
        <>
            <StatusStrip
                mode="COLLECT"
                tone="amber"
                live={{
                    ok: live,
                    label: live
                        ? "REAL ROBOT · LIVE"
                        : occupiedByInfer
                        ? "当前为推理会话"
                        : inCapture
                        ? "本页未持租约"
                        : "准备采集",
                }}
                stage={recording ? "REC" : undefined}
            />
            <PageLayout
                left={
                    <>
                        <MetaCard />
                        <SessionDeck
                            api={api}
                            hasLease={hasLease}
                            nodeState={nodeState}
                            sessionType={sessionType}
                            captures={captures}
                            infers={infers}
                            policies={health?.adapters.policies ?? []}
                            onLog={onLog}
                            fixedKind="capture"
                        />
                        <SessionHooks />
                    </>
                }
                center={
                    <>
                        <div className={threeEnabled ? "min-h-[18rem] flex-1" : "shrink-0"}>
                            <TelemetryViewport mode={recording ? "REC" : "LIVE"} />
                        </div>
                        <ObservationCard />
                    </>
                }
                right={<CameraRail />}
            />
        </>
    );
}
