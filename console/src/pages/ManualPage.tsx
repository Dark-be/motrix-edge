import { CameraRail } from "../layout/CameraRail";
import { PageLayout } from "../layout/PageLayout";
import { ManualControl } from "../panels/ManualControl";
import { ObservationCard } from "../panels/ObservationCard";
import { SessionHooks } from "../panels/SessionHooks";
import { StatusStrip } from "../shell/StatusStrip";
import { TelemetryViewport } from "../viewport/TelemetryViewport";
import { useEdge } from "../edge";

/**
 * 01 MANUAL —— 手动操作页。
 *
 * 左：动作空间滑块（拖动只改目标，SEND 才下发）+ 可选接入；
 * 中：3D（实测臂 + 目标臂 / 目标点 + 实测轨迹）；右：相机。
 */
export function ManualPage() {
    const { hasLease, preview, adapter, infers, threeEnabled } = useEdge();
    const step = infers?.rtc?.index;
    const live = hasLease && preview !== null;

    return (
        <>
            <StatusStrip
                mode="MANUAL"
                tone="blue"
                live={{ ok: live, label: live ? "REAL ROBOT · LIVE" : "等待租约 / 观测" }}
                stage={adapter?.teleop ? "接管" : "程控"}
            />
            <PageLayout
                left={
                    <>
                        <ManualControl />
                        <SessionHooks />
                    </>
                }
                center={
                    <>
                        <div className={threeEnabled ? "min-h-[20rem] flex-1" : "shrink-0"}>
                            <TelemetryViewport
                                mode={adapter?.teleop ? "TAKEOVER" : "STAGE"}
                                footer={`STEP ${step ?? "—"}`}
                                manual
                            />
                        </div>
                        <ObservationCard />
                    </>
                }
                right={<CameraRail />}
            />
        </>
    );
}
