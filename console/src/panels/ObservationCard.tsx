import type { ReactNode } from "react";

import { useEdge } from "../edge";
import { Card } from "../components/ui";
import { splitByArms } from "../viewport/kinematics";

/** 数值 3 位小数（与日志口径一致）。 */
const fmt = (v: number) => v.toFixed(3);

/** 每臂 6 维（xyz + rpy）→ `pos=[…] rpy=[…]`；维度不足退化成纯数值。 */
const fmtPose = (values: number[]) =>
    values.length >= 6
        ? `pos=[${values.slice(0, 3).map(fmt).join(", ")}] rpy=[${values.slice(3, 6).map(fmt).join(", ")}]`
        : values.map(fmt).join(", ");

const armTone: Record<string, string> = { LEFT: "text-sky-400", RIGHT: "text-amber-400" };

function Row({
    field,
    values,
    arms,
    format,
}: {
    field: string;
    values?: number[] | null;
    arms: string[];
    format: (v: number[]) => string;
}) {
    const labels = arms.length > 0 ? arms.map((a) => a.toUpperCase()) : [""];
    const segments = splitByArms(values, arms.length > 0 ? arms : [""]);
    return (
        <div className="flex items-baseline gap-3 overflow-x-auto whitespace-nowrap rounded border border-line bg-panel2 px-2 py-1">
            {segments.length === 0 ? (
                <span className="text-[11px] text-faint">
                    <span className="uppercase tracking-wide">{field}</span>: —
                </span>
            ) : (
                segments.map((values, i) => (
                    <span
                        key={`${labels[i]}-${i}`}
                        className={`flex items-baseline gap-1.5 ${i > 0 ? "border-l border-line pl-3" : ""}`}
                    >
                        {labels[i] && (
                            <span
                                className={`text-[10px] uppercase tracking-wide ${armTone[labels[i]] ?? "text-faint"}`}
                            >
                                {labels[i]}
                            </span>
                        )}
                        <span className="text-[10px] uppercase tracking-wide text-faint">{field}</span>
                        <span className="tnum font-mono text-[11px] text-ink">{format(values)}</span>
                    </span>
                ))
            )}
        </div>
    );
}

/** 观测数值卡：qpos / 夹爪 / qpos_err / 最新动作 / EEF 实测与目标（图像走 WebRTC）。 */
export function ObservationCard({ actions }: { actions?: ReactNode }) {
    const { preview, arms, previewError, hasLease } = useEdge();
    const obs = preview?.observation;

    return (
        <Card title="Preview" actions={actions}>
            {!hasLease ? (
                <p className="text-[11px] text-muted">需租约</p>
            ) : (
                <div className="space-y-1">
                    <Row field="qpos" values={obs?.qpos} arms={arms} format={(v) => v.map(fmt).join(", ")} />
                    <Row field="gripper" values={obs?.gripper} arms={arms} format={(v) => v.map(fmt).join(", ")} />
                    <Row field="qpos err" values={obs?.qpos_err} arms={arms} format={(v) => v.map(fmt).join(", ")} />
                    <Row field="action" values={obs?.action} arms={arms} format={(v) => v.map(fmt).join(", ")} />
                    <Row field="EEF 实测" values={obs?.pose} arms={arms} format={fmtPose} />
                    <Row field="EEF 目标" values={obs?.pose_target} arms={arms} format={fmtPose} />
                </div>
            )}
            {previewError && <p className="mt-1 text-[11px] text-rose-400">{previewError}</p>}
        </Card>
    );
}
