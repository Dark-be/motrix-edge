import { lazy, Suspense } from "react";

import { Button } from "../components/ui";
import { useEdge } from "../edge";
import { useTheme } from "../theme";

// 只有用户开启 3D 后才下载包含 Three.js 的视口 chunk；关闭时不会创建 WebGL renderer 或动画循环。
const Viewport3D = lazy(async () => {
    const module = await import("./Viewport3D");
    return { default: module.Viewport3D };
});

/**
 * 中心 3D 视口的**数据接线**：从全局状态取实测姿态 / 期望轨迹 / 手动目标，交给 `Viewport3D`
 * 画图——各页面只关心 `mode`（角标）与 `footer`（左下角标签）。
 *
 * 所有页面共用 Robot action 的蓝色半透明目标臂；`manual` 只决定是否叠加手动暂存轨迹，
 * 避免把手动页暂存值误当成 Robot 已接收的 action。
 */
export function TelemetryViewport({
    mode,
    footer,
    manual = false,
}: {
    mode: string;
    footer?: string;
    manual?: boolean;
}) {
    const { arms, preview, trajectory, threeEnabled, setThreeEnabled } = useEdge();
    const { theme } = useTheme();
    const obs = preview?.observation;
    const chunk = manual
        ? trajectory?.source === "manual target"
            ? trajectory
            : null
        : trajectory?.source === "infer rollout"
        ? trajectory
        : null;

    if (!threeEnabled) {
        return (
            <div className="flex min-h-[4.5rem] items-center justify-center rounded-lg border border-line bg-panel2 px-3">
                <div className="flex flex-wrap items-center justify-center gap-2 text-center">
                    <p className="text-[11px] text-muted">3D 已关闭，不占用 WebGL / GPU。</p>
                    <Button size="xs" variant="primary" onClick={() => setThreeEnabled(true)}>
                        开启 3D
                    </Button>
                </div>
            </div>
        );
    }

    return (
        <Suspense
            fallback={
                <div className="flex min-h-[18rem] flex-1 items-center justify-center rounded-lg border border-line bg-panel2 text-[12px] text-muted">
                    正在加载 3D 组件…
                </div>
            }
        >
            <Viewport3D
                arms={arms}
                qpos={obs?.qpos ?? null}
                gripper={obs?.gripper ?? null}
                action={obs?.action ?? null}
                chunk={chunk}
                mode={mode}
                footer={footer}
                theme={theme}
            />
        </Suspense>
    );
}
