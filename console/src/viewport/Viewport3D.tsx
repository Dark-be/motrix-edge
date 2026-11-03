import { useEffect, useMemo, useRef, useState } from "react";

import type { ChunkTrajectory } from "../edge";
import { PALETTES, ArmScene, type ArmState, type Polyline } from "./armScene";
import { ARM_DOF, fitJoints, fkEEF, splitByArms, type Vec3 } from "./kinematics";

export interface Viewport3DProps {
    /** 启用臂名（空 → 单臂） */
    arms: string[];
    /** 实测关节（扁平，按 `arms` 切段） */
    qpos: number[] | null;
    /** 实测夹爪（每臂 1 维，0–1） */
    gripper: number[] | null;
    /** Robot 当前 action（每臂 `[joint6, gripper1]`）→ 蓝色半透明目标臂 */
    action: number[] | null;
    /** 期望轨迹（推理动作块 / 手动目标） */
    chunk: ChunkTrajectory | null;
    /** 右上角模式角标（LIVE / STAGE / INFER…） */
    mode: string;
    /** 左下角标签（STEP / CHUNK / 帧计数等） */
    footer?: string;
    theme: "light" | "dark";
}

/**
 * 中心 3D 视口：机械臂（连杆 + 关节点，不加载 URDF）+ **期望轨迹**。
 *
 * 姿态与轨迹用同一份 DH 正解算（kinematics），因此天然对齐；**只画下发目标推导出的
 * 轨迹**——实测末端位姿走的是机器人自己的位姿系，与这里的简化臂不同源，画在同一场景
 * 里会看起来「偏一截」。
 */
export function Viewport3D({ arms, qpos, gripper, action, chunk, mode, footer, theme }: Viewport3DProps) {
    const hostRef = useRef<HTMLDivElement | null>(null);
    const sceneRef = useRef<ArmScene | null>(null);
    const [ready, setReady] = useState(false);
    const [glError, setGlError] = useState<string | null>(null);
    /** 首帧建场用的主题（后续切换只改颜色，不重建上下文） */
    const initialTheme = useRef(theme);

    const armNames = arms.length > 0 ? arms : [""];

    // ---- 实测姿态 ----
    const currentStates = useMemo<ArmState[]>(() => {
        const joints = splitByArms(qpos, armNames);
        const grips = splitByArms(gripper, armNames);
        return armNames.map((_, i) => ({ joints: fitJoints(joints[i]), gripper: grips[i]?.[0] ?? null }));
    }, [qpos, gripper, armNames]);

    // ---- Robot 当前目标姿态：action 每臂固定为 [joint6, gripper1] ----
    const targetStates = useMemo<ArmState[] | null>(() => {
        if (!action) return null;
        const blocks = splitByArms(action, armNames);
        return armNames.map((_, i) => {
            const block = blocks[i] ?? [];
            return {
                joints: fitJoints(block.slice(0, ARM_DOF)),
                gripper: block.length > ARM_DOF ? block[ARM_DOF] : null,
            };
        });
    }, [action, armNames]);

    // ---- 期望轨迹（动作块 / 手动目标）→ 每臂末端折线 ----
    const trajectories = useMemo<Polyline[]>(() => {
        if (!chunk || chunk.actions.length === 0) return [];
        const deltaBase: (Vec3 | null)[] = [];
        if (chunk.layout === "pose_delta") {
            const joints = splitByArms(qpos, armNames);
            armNames.forEach((_, i) => deltaBase.push(joints[i] ? fkEEF(fitJoints(joints[i])).xyz : null));
        }
        const accumulate: Vec3[][] = armNames.map(() => []);
        chunk.actions.forEach((step) => {
            const perArm = splitByArms(step, armNames);
            armNames.forEach((_, i) => {
                const values = perArm[i] ?? [];
                if (chunk.layout === "pose" || chunk.layout === "pose_delta") {
                    const base = chunk.layout === "pose" ? [0, 0, 0] : deltaBase[i] ?? [0, 0, 0];
                    const previous = accumulate[i][accumulate[i].length - 1] ?? base;
                    accumulate[i].push([
                        previous[0] + (values[0] ?? 0),
                        previous[1] + (values[1] ?? 0),
                        previous[2] + (values[2] ?? 0),
                    ]);
                } else {
                    accumulate[i].push(fkEEF(fitJoints(values)).xyz);
                }
            });
        });
        return armNames
            .map((_, i) => ({ armIndex: i, points: accumulate[i], color: PALETTES[theme].trajectory }))
            .filter((line) => line.points.length >= 1);
    }, [chunk, qpos, armNames, theme]);

    useEffect(() => {
        if (!hostRef.current) return;
        let scene: ArmScene;
        try {
            scene = new ArmScene(hostRef.current, initialTheme.current);
        } catch (e) {
            // 无 WebGL（无 GPU 的容器 / 老浏览器）：给出可读提示，不让整页崩
            setGlError(e instanceof Error ? e.message : String(e));
            return;
        }
        sceneRef.current = scene;
        setGlError(null);
        setReady(true);
        return () => {
            scene.dispose();
            sceneRef.current = null;
            setReady(false);
        };
    }, []);

    // 主题切换只换颜色 / 网格，不重建 WebGL 上下文
    useEffect(() => {
        sceneRef.current?.setTheme(theme);
    }, [theme, ready]);

    useEffect(() => {
        if (!ready) return;
        sceneRef.current?.setArms(currentStates, targetStates);
    }, [ready, currentStates, targetStates]);

    useEffect(() => {
        if (!ready) return;
        sceneRef.current?.setPolylines(trajectories);
    }, [ready, trajectories]);

    return (
        <div className="relative h-full min-h-[16rem] w-full overflow-hidden rounded-md border border-line bg-panel2">
            <div ref={hostRef} className="h-full w-full" />
            {glError && (
                <div className="absolute inset-0 flex items-center justify-center p-4 text-center text-[11px] text-muted">
                    3D 不可用（WebGL 初始化失败）
                </div>
            )}
            <div className="pointer-events-none absolute right-2 top-2 flex items-center gap-1.5">
                <span className="rounded bg-panel/80 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted">
                    3D · FK
                </span>
                <span className="rounded bg-accent-soft px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-accent">
                    {mode}
                </span>
            </div>
            <div className="pointer-events-none absolute bottom-2 left-2 flex items-center gap-3 text-[10px] text-muted">
                {footer && <span className="tnum rounded bg-panel/80 px-1.5 py-0.5">{footer}</span>}
                <span className="flex items-center gap-1">
                    <i className="inline-block h-[2px] w-4 bg-accent" /> arm
                </span>
                {targetStates && (
                    <span className="flex items-center gap-1">
                        <i className="inline-block h-[2px] w-4 bg-sky-500 opacity-70" /> action
                    </span>
                )}
                {trajectories.length > 0 && (
                    <span className="flex items-center gap-1">
                        <i className="inline-block h-[2px] w-4 bg-emerald-500" /> traj
                    </span>
                )}
            </div>
            <button
                type="button"
                onClick={() => sceneRef.current?.resetView()}
                title="复位视角（拖动旋转 · 滚轮缩放）"
                className="absolute bottom-2 right-2 rounded border border-line bg-panel/80 px-1.5 py-0.5 text-[10px] text-muted hover:text-ink"
            >
                复位
            </button>
        </div>
    );
}
