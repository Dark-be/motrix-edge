import { useCallback, useEffect, useMemo, useState } from "react";

import { useEdge, type ChunkTrajectory } from "../edge";
import { useAdapterInfo } from "../hooks/useAdapterInfo";
import { Badge, Button, Card, Segmented, Switch } from "../components/ui";
import { jointLimit, splitByArms } from "../viewport/kinematics";

/** 可直控的动作空间（`robot execute` 的 `layout` 词表；本页只实现这四种显示方式）。 */
const SPACES = [
    { value: "joint", label: "joint", title: "关节角（绝对目标，弧度）" },
    { value: "pose", label: "pose", title: "末端位姿（绝对目标，xyz 米 + rpy 弧度）" },
    { value: "pose_delta", label: "Δpose", title: "末端位姿增量（叠加在当前关节段目标上再解一次）" },
    { value: "gripper", label: "grip", title: "夹爪开合（归一化 0–1，1 = 张开）" },
] as const;

type Space = (typeof SPACES)[number]["value"];

/** 位姿 / 位姿增量的点按步长（米或弧度，按当前维度语义解释）。 */
const POSE_STEPS = ["0.05", "0.01", "0.005"] as const;

/** 每臂每段的量程与步长（joint 用关节限位；pose 用工作空间近似范围）。 */
function rangeFor(space: Space, index: number): { min: number; max: number; step: number } {
    if (space === "joint") {
        const [min, max] = jointLimit(index);
        return { min, max, step: 0.001 };
    }
    if (space === "gripper") return { min: 0, max: 1, step: 0.01 };
    const isDelta = space === "pose_delta";
    if (index < 3) {
        return isDelta ? { min: -0.2, max: 0.2, step: 0.0005 } : { min: -0.8, max: 0.8, step: 0.001 };
    }
    return isDelta ? { min: -0.8, max: 0.8, step: 0.0005 } : { min: -Math.PI, max: Math.PI, step: 0.001 };
}

const LABELS: Record<Space, string[]> = {
    joint: ["j0", "j1", "j2", "j3", "j4", "j5"],
    pose: ["x", "y", "z", "r", "p", "y"],
    pose_delta: ["Δx", "Δy", "Δz", "Δr", "Δp", "Δy"],
    gripper: ["g"],
};

const fmt = (v: number) => v.toFixed(3);

/**
 * 手动操作面板：**编辑目标 → 点 SEND 才下发**（编辑过程不发任何指令）。
 *
 * `joint` / `gripper` 使用滑块；`pose` / `pose_delta` 使用「− 数值 +」按所选 step 点按微调；
 * 可勾选叠加夹爪段（layout = `joint+gripper`）。按钮只有四件：发送 / 填充 HOME /
 * 填充 QPOS / 急停——前三个都不下发，最后一个全局安全动作。
 */
export function ManualControl() {
    const { api, arms, preview, hasLease, run, estop, setTrajectory, setManualTarget, adapter, nodeState } = useEdge();
    const { details, current } = useAdapterInfo();

    const armNames = arms.length > 0 ? arms : [""];
    const armCount = armNames.length;
    const available = details?.capabilities?.action_spaces ?? null;

    const [space, setSpace] = useState<Space>("joint");
    const [poseStep, setPoseStep] = useState<(typeof POSE_STEPS)[number]>("0.01");
    /** 每臂的值段目标（未含夹爪） */
    const [targets, setTargets] = useState<number[][] | null>(null);
    /** 每臂夹爪目标（勾选「含夹爪」时下发） */
    const [grippers, setGrippers] = useState<number[] | null>(null);
    const [includeGripper, setIncludeGripper] = useState(false);

    const gripperAvailable = available === null || available.includes("gripper");
    const canIncludeGripper = gripperAvailable && space !== "gripper";

    /** 每臂值段维度：优先用 adapter 上报的 `action_dims`，否则按空间约定。 */
    const dimPerArm = useMemo(() => {
        const reported = current?.action_dims?.[space];
        if (reported && reported > 0 && reported % armCount === 0) return reported / armCount;
        return space === "gripper" ? 1 : 6;
    }, [current, space, armCount]);

    /** 填充：关节用实测 qpos / 位姿用实测 pose / 增量一律 0；夹爪取实测值。 */
    const fillFrom = useCallback(
        (source: "qpos" | "home") => {
            const obs = preview?.observation;
            const perArm =
                source === "home"
                    ? splitByArms(current?.home?.[space] ?? null, armNames)
                    : splitByArms(
                          space === "joint"
                              ? obs?.qpos
                              : space === "pose" || space === "pose_delta"
                              ? obs?.pose
                              : obs?.gripper,
                          armNames
                      );
            if (perArm.length === 0) return;
            setTargets(perArm.map((values) => values.slice(0, dimPerArm)));
            setGrippers(splitByArms(obs?.gripper ?? null, armNames).map((values) => values[0] ?? 0));
        },
        [preview, current, space, armNames, dimPerArm]
    );

    // 换动作空间 / 换臂数 / 换维度 → 目标作废，重新填当前值（增量空间从 0 起）
    useEffect(() => {
        setTargets(null);
        setGrippers(null);
    }, [space, armCount, dimPerArm]);

    useEffect(() => {
        if (targets !== null) return;
        if (space === "pose_delta") {
            setTargets(armNames.map(() => new Array(dimPerArm).fill(0)));
            setGrippers(splitByArms(preview?.observation.gripper ?? null, armNames).map((v) => v[0] ?? 0));
            return;
        }
        fillFrom("qpos");
    }, [targets, space, armNames, dimPerArm, preview, fillFrom]);

    // 目标 → 下发向量（layout 段序列：值段 [+ 夹爪段]）与 3D 展示数据
    const pieces = useMemo(() => {
        if (!targets) return null;
        const flat: number[] = [];
        armNames.forEach((_, i) => {
            const values = targets[i] ?? [];
            for (let k = 0; k < dimPerArm; k += 1) flat.push(values[k] ?? 0);
            if (includeGripper && canIncludeGripper) flat.push(grippers?.[i] ?? 0);
        });
        const layout = includeGripper && canIncludeGripper ? `${space}+gripper` : space;
        return { flat, layout };
    }, [targets, grippers, includeGripper, canIncludeGripper, space, dimPerArm, armNames]);

    // 目标变化 → 同步给 3D 视口（目标臂 / 期望轨迹）
    useEffect(() => {
        if (!pieces) return;
        setManualTarget({ space, layout: pieces.layout, flat: pieces.flat });
        const chunk: ChunkTrajectory = {
            actions: [pieces.flat],
            layout: pieces.layout,
            step: null,
            source: "manual target",
            at: Date.now(),
        };
        setTrajectory(chunk);
    }, [pieces, space, setManualTarget, setTrajectory]);

    const blocked = !hasLease
        ? "需租约"
        : nodeState === "idle"
        ? "节点未就绪"
        : nodeState === "error"
        ? "节点 ERROR"
        : null;

    const onSend = () => {
        if (!pieces || !api) return;
        void run(`robot execute ${pieces.layout}`, () => api.robotExecute(pieces.flat, pieces.layout));
    };

    const usesPoseStep = space === "pose" || space === "pose_delta";
    const updateTarget = (armIndex: number, index: number, next: number) => {
        setTargets((prev) => {
            const copy = (prev ?? []).map((row) => [...row]);
            while (copy.length < armCount) copy.push([]);
            copy[armIndex] = copy[armIndex] ?? [];
            copy[armIndex][index] = Number(next.toFixed(6));
            return copy;
        });
    };

    return (
        <Card
            title="Manual"
            actions={
                <Badge tone={adapter?.teleop ? "amber" : "slate"}>
                    {adapter?.teleop ? (adapter.teleop_mode === "delta" ? "接管" : "示教") : "程控"}
                </Badge>
            }
        >
            <div className="space-y-2">
                <Segmented
                    value={space}
                    options={SPACES.map((s) => ({
                        value: s.value,
                        label: s.label,
                        title: s.title,
                        disabled: available !== null && !available.includes(s.value),
                    }))}
                    onChange={setSpace}
                />

                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-faint">
                    <span>
                        dim <span className="tnum font-mono text-ink">{dimPerArm}</span> × {armCount} ={" "}
                        <span className="tnum font-mono text-ink">{pieces?.flat.length ?? 0}</span>
                    </span>
                    <span>layout {pieces?.layout ?? space}</span>
                    {usesPoseStep && (
                        <span className="flex items-center gap-1">
                            step
                            <Segmented
                                value={poseStep}
                                options={POSE_STEPS.map((value) => ({ value, label: value }))}
                                onChange={setPoseStep}
                            />
                        </span>
                    )}
                </div>

                {canIncludeGripper && (
                    <Switch
                        checked={includeGripper}
                        onChange={setIncludeGripper}
                        label="含夹爪"
                        title={`同时下发夹爪段：layout = ${space}+gripper`}
                    />
                )}

                {targets === null ? (
                    <p className="text-[11px] text-muted">等待观测…</p>
                ) : (
                    <div className="space-y-2">
                        {armNames.map((arm, armIndex) => (
                            <div key={arm || armIndex} className="space-y-0.5">
                                <span className="text-[10px] font-semibold uppercase tracking-wide text-accent">
                                    {arm || `arm${armIndex}`}
                                </span>
                                {Array.from({ length: dimPerArm }, (_, index) => {
                                    const range = rangeFor(space, index);
                                    const value = targets[armIndex]?.[index] ?? 0;
                                    return (
                                        <label key={index} className="flex items-center gap-2">
                                            <span className="w-6 shrink-0 font-mono text-[10px] text-faint">
                                                {LABELS[space][index] ?? `v${index}`}
                                            </span>
                                            {usesPoseStep ? (
                                                <div className="flex min-w-0 flex-1 items-center gap-1">
                                                    <button
                                                        type="button"
                                                        className="flex h-6 w-7 shrink-0 items-center justify-center rounded border border-line2 bg-panel2 text-sm font-semibold text-ink hover:border-accent hover:text-accent"
                                                        title={`减 ${poseStep}`}
                                                        onClick={() =>
                                                            updateTarget(
                                                                armIndex,
                                                                index,
                                                                Math.max(range.min, value - Number(poseStep))
                                                            )
                                                        }
                                                    >
                                                        −
                                                    </button>
                                                    <span className="tnum min-w-0 flex-1 text-center font-mono text-[11px] text-ink">
                                                        {fmt(value)}
                                                    </span>
                                                    <button
                                                        type="button"
                                                        className="flex h-6 w-7 shrink-0 items-center justify-center rounded border border-line2 bg-panel2 text-sm font-semibold text-ink hover:border-accent hover:text-accent"
                                                        title={`加 ${poseStep}`}
                                                        onClick={() =>
                                                            updateTarget(
                                                                armIndex,
                                                                index,
                                                                Math.min(range.max, value + Number(poseStep))
                                                            )
                                                        }
                                                    >
                                                        +
                                                    </button>
                                                </div>
                                            ) : (
                                                <>
                                                    <input
                                                        type="range"
                                                        className="h-1 min-w-0 flex-1 accent-[var(--t-accent)]"
                                                        min={range.min}
                                                        max={range.max}
                                                        step={range.step}
                                                        value={value}
                                                        onChange={(e) =>
                                                            updateTarget(armIndex, index, Number(e.target.value))
                                                        }
                                                    />
                                                    <span className="tnum w-12 shrink-0 text-right font-mono text-[11px] text-ink">
                                                        {fmt(value)}
                                                    </span>
                                                </>
                                            )}
                                        </label>
                                    );
                                })}
                                {includeGripper && canIncludeGripper && (
                                    <label className="flex items-center gap-2">
                                        <span className="w-6 shrink-0 font-mono text-[10px] text-faint">g</span>
                                        <input
                                            type="range"
                                            className="h-1 min-w-0 flex-1 accent-[var(--t-accent)]"
                                            min={0}
                                            max={1}
                                            step={0.01}
                                            value={grippers?.[armIndex] ?? 0}
                                            onChange={(e) => {
                                                const next = Number(e.target.value);
                                                setGrippers((prev) => {
                                                    const copy = [...(prev ?? new Array(armCount).fill(0))];
                                                    copy[armIndex] = next;
                                                    return copy;
                                                });
                                            }}
                                        />
                                        <span className="tnum w-12 shrink-0 text-right font-mono text-[11px] text-ink">
                                            {fmt(grippers?.[armIndex] ?? 0)}
                                        </span>
                                    </label>
                                )}
                            </div>
                        ))}
                    </div>
                )}

                <div className="space-y-1.5 border-t border-line pt-2">
                    <Button
                        variant="primary"
                        className="w-full"
                        disabled={!pieces || blocked !== null}
                        title={blocked ?? "robot execute：下发当前目标"}
                        onClick={onSend}
                    >
                        ▶ SEND
                    </Button>
                    <div className="grid grid-cols-2 gap-1.5">
                        <Button
                            size="xs"
                            disabled={!current?.home?.[space]}
                            title="把 home 值填进滑块（不下发）"
                            onClick={() => fillFrom("home")}
                        >
                            填充 HOME
                        </Button>
                        <Button size="xs" title="把实测值填进滑块（不下发）" onClick={() => fillFrom("qpos")}>
                            填充 QPOS
                        </Button>
                    </div>
                    <Button
                        variant="danger"
                        className="w-full"
                        disabled={!hasLease}
                        title="robot/estop（快捷键 Esc）"
                        onClick={estop}
                    >
                        ■ 急停
                    </Button>
                    {blocked && <p className="text-[10px] text-amber-400">{blocked}</p>}
                </div>
            </div>
        </Card>
    );
}
