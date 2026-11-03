import { useEffect, useMemo, useState } from "react";

import { useEdge } from "../edge";
import { Button, Card, Segmented, Switch } from "../components/ui";

const POSE_AXES = ["x", "y", "z", "roll", "pitch", "yaw"] as const;
type FollowSpace = "joint" | "pose";

/**
 * 「可选接入」卡：会话页（数采 / 推理 / 手动）按需接入的旁路能力。
 *
 * - **人工接管**（`robot takeover` = teleop `delta`）：接管瞬间不突变，只叠加主臂增量；
 *   推理会话中开启会让 rollout 被机器人进程拒绝（进程侧保护）；
 * - **示教**（`robot teach` = teleop `absolute`）：主臂绝对读数直连从臂；
 * - 两者互斥（同一时刻只有一种遥操作位）；关闭即回到程控；
 * - **机器人复位**（`robot reset`，回 home）与**急停**（`robot/estop`）。
 */
export function SessionHooks() {
    const { api, adapter, hasLease, run, estop, nodeState } = useEdge();
    const teleop = adapter?.teleop ?? false;
    const mode = adapter?.teleop_mode ?? null;
    const reportedSpace: FollowSpace = adapter?.teleop_follow_space === "pose" ? "pose" : "joint";
    const [followSpace, setFollowSpace] = useState<FollowSpace>(reportedSpace);
    useEffect(() => setFollowSpace(reportedSpace), [reportedSpace]);
    const poseTeleop = followSpace === "pose";
    const reportedAxes = reportedSpace === "pose" ? adapter?.teleop_follow_axes ?? [...POSE_AXES] : [...POSE_AXES];
    const reportedKey = reportedAxes.join(",");
    const [lockedAxes, setLockedAxes] = useState<string[]>([]);
    useEffect(() => {
        const followed = new Set(reportedAxes);
        setLockedAxes(POSE_AXES.filter((axis) => !followed.has(axis)));
    }, [poseTeleop, reportedKey]);
    const followAxes = useMemo(() => POSE_AXES.filter((axis) => !lockedAxes.includes(axis)), [lockedAxes]);
    const allLocked = poseTeleop && followAxes.length === 0;
    const busy = !hasLease || nodeState === "idle";
    const reason = !hasLease ? "需先持有租约" : nodeState === "idle" ? "机器人进程未就绪" : undefined;

    const setTeleop = (enabled: boolean, next: "absolute" | "delta") => {
        if (!api) return;
        const axes = enabled && poseTeleop ? [...followAxes] : undefined;
        const command =
            next === "delta"
                ? api.robotTakeover(enabled, axes, followSpace)
                : api.robotTeach(enabled, axes, followSpace);
        void run(`${enabled ? "开启" : "关闭"}${next === "delta" ? "人工接管" : "示教"}`, () => command);
    };

    const toggleAxis = (axis: string) => {
        setLockedAxes((current) =>
            current.includes(axis) ? current.filter((item) => item !== axis) : [...current, axis]
        );
    };

    return (
        <Card title="Hooks">
            <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-[11px] text-muted">跟随空间</span>
                    <Segmented<FollowSpace>
                        value={followSpace}
                        options={[
                            { value: "joint", label: "Joint", title: "直接映射关节角" },
                            { value: "pose", label: "Pose", title: "FK → 位姿约束 → IK，可锁定轴" },
                        ]}
                        onChange={setFollowSpace}
                        disabled={busy || teleop}
                    />
                </div>
                <Switch
                    checked={teleop && mode === "delta"}
                    onChange={(next) => setTeleop(next, "delta")}
                    label="人工接管（增量）"
                    title={reason ?? "robot takeover：以接管瞬间位姿为锚点，只叠加主臂增量（推理会被进程拒绝）"}
                    disabled={busy || (!teleop && allLocked)}
                />
                <Switch
                    checked={teleop && mode !== "delta"}
                    onChange={(next) => setTeleop(next, "absolute")}
                    label="示教（绝对）"
                    title={reason ?? "robot teach：主臂绝对读数直连从臂"}
                    disabled={busy || (!teleop && allLocked)}
                />
                {poseTeleop && (
                    <fieldset className="rounded border border-line bg-panel/40 px-2 pb-2 pt-1.5">
                        <legend className="px-1 text-[10px] uppercase tracking-wide text-faint">Pose 锁定轴</legend>
                        <div className="grid grid-cols-3 gap-x-2 gap-y-1">
                            {POSE_AXES.map((axis) => (
                                <label
                                    key={axis}
                                    className="flex cursor-pointer items-center gap-1.5 text-[11px] text-muted"
                                >
                                    <input
                                        type="checkbox"
                                        checked={lockedAxes.includes(axis)}
                                        disabled={busy || teleop}
                                        onChange={() => toggleAxis(axis)}
                                        className="h-3.5 w-3.5 accent-amber-500 disabled:opacity-40"
                                    />
                                    <span className="font-mono">{axis}</span>
                                </label>
                            ))}
                        </div>
                        <div className={`mt-1 text-[10px] ${allLocked ? "text-rose-300" : "text-faint"}`}>
                            {teleop
                                ? "锁轴已生效；先关闭遥操作才能修改并重采锚点"
                                : allLocked
                                ? "至少保留一个跟随轴"
                                : "勾选轴将在下次开启时锁定于从臂当前位姿"}
                        </div>
                    </fieldset>
                )}
                <div className="flex flex-wrap items-center gap-1.5 border-t border-line pt-2">
                    <Button
                        size="xs"
                        disabled={busy}
                        title={reason ?? "robot reset：回 home（非阻塞）"}
                        onClick={() => void run("robot reset", () => api!.robotReset())}
                    >
                        机器人复位
                    </Button>
                    <Button
                        size="xs"
                        variant="danger"
                        disabled={!hasLease}
                        title="robot/estop（快捷键 Esc）"
                        onClick={estop}
                    >
                        ■ 急停
                    </Button>
                    <span className="text-[10px] text-faint">接管 / 示教互斥；关闭即回程控</span>
                </div>
            </div>
        </Card>
    );
}
