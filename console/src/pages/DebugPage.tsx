import { useEdge } from "../edge";
import { CameraRail } from "../layout/CameraRail";
import { PageLayout } from "../layout/PageLayout";
import { ObservationCard } from "../panels/ObservationCard";
import { StatusStrip } from "../shell/StatusStrip";
import { TelemetryViewport } from "../viewport/TelemetryViewport";
import { AdapterPanel } from "../components/AdapterPanel";
import { RpentPanel } from "../components/RpentPanel";
import { UploadPanel } from "../components/UploadPanel";

/**
 * 04 DEBUG —— 调试页：适配器能力 / 原语 / 深度 / 上传 + 观测数值与 3D。
 *
 * 这里保留「原始信息尽量完整」的旧控制台能力（租约 / 节点状态的**细节**已收进顶栏下拉，
 * 页面只放操作与诊断）；三个栏位都是可滚动卡片，缺可选依赖时面板自己出提示。
 */
export function DebugPage() {
    const { api, adapter, hasLease, onLog, preview, nodeState, threeEnabled } = useEdge();

    return (
        <>
            <StatusStrip
                mode="DEBUG"
                tone="slate"
                live={{ ok: hasLease, label: hasLease ? "诊断模式 · 受控操作可用" : "诊断模式 · 受控操作需租约" }}
                stage={`NODE ${nodeState ?? "—"}`}
            />
            <PageLayout
                left={
                    <>
                        <AdapterPanel api={api} adapter={adapter} hasLease={hasLease} onLog={onLog} />
                        <RpentPanel api={api} hasLease={hasLease} onLog={onLog} />
                        {api && <UploadPanel api={api} onLog={onLog} />}
                    </>
                }
                center={
                    <>
                        <div className={threeEnabled ? "min-h-[20rem] flex-1" : "shrink-0"}>
                            <TelemetryViewport
                                mode="LIVE"
                                footer={preview?.state ? `SESSION ${preview.state}` : undefined}
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
