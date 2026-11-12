import { useEffect } from "react";

import { PREVIEW_RATES, useEdge } from "../edge";
import { Button, fmtDuration } from "../components/ui";
import { useTheme } from "../theme";
import { ConnectionMenu } from "./ConnectionMenu";
import { AuthMenu } from "./AuthMenu";
import { LeaseMenu } from "./LeaseMenu";
import { NodeMenu } from "./NodeMenu";
import { Stat } from "./controls";
import { useElapsed } from "./useElapsed";

/**
 * 顶部常驻状态条：品牌 + 遥测（机器人 / 控制频率 / 节点状态 / 推理耗时 / 步号 / 计时）
 * + 右侧下拉（连接 / 租约 / 节点）+ 主题切换 + 全局急停。
 *
 * **整条强制深色**（`dark` 类）：牌子区与浅色内容区形成对比；子树的主题令牌全部走深色一套，
 * 所以里面的组件不用另写配色。
 */
export function TopBar() {
    const {
        connected,
        health,
        adapter,
        hasLease,
        estop,
        since,
        previewHz,
        setPreviewHz,
        threeEnabled,
        setThreeEnabled,
    } = useEdge();
    const { theme, toggle } = useTheme();
    const elapsed = useElapsed(connected ? since : null);

    const tx = adapter?.measured_hz ?? adapter?.control_hz ?? null;

    // Esc = 急停（须持租约；输入框 / 下拉聚焦时不抢键）
    useEffect(() => {
        if (!connected || !hasLease) return;
        const onKey = (e: KeyboardEvent) => {
            const el = e.target as HTMLElement | null;
            const typing = !!el && (el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA");
            if (e.key === "Escape" && !typing) estop();
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [connected, hasLease, estop]);

    return (
        <header className="dark flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-page px-3 py-1.5 text-ink">
            <div className="flex items-center gap-2">
                <span className="text-[13px] font-semibold tracking-[0.18em] text-ink">MOTRIX EDGE</span>
                <span className="rounded bg-accent-soft px-1.5 py-0.5 text-[10px] font-medium tracking-wide text-accent">
                    CONSOLE
                </span>
            </div>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-l border-line pl-4">
                <Stat label="robot" value={adapter?.name ?? health?.robot?.name ?? "—"} />
                <Stat label="tx" value={tx !== null ? `${Number(tx).toFixed(1)}Hz` : "—"} />
                <Stat label="time" value={elapsed !== null ? fmtDuration(elapsed) : "—"} />
            </div>

            <div className="ml-auto flex flex-wrap items-center gap-1.5">
                {threeEnabled && (
                    <label className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-faint">
                        3D Hz
                        <select
                            className="rounded border border-line bg-panel2 px-1 py-0.5 font-mono text-[11px] text-ink focus:border-accent focus:outline-none"
                            value={previewHz}
                            title="3D 观测刷新频率"
                            onChange={(e) => setPreviewHz(Number(e.target.value))}
                        >
                            {PREVIEW_RATES.map((hz) => (
                                <option key={hz} value={hz}>
                                    {hz}
                                </option>
                            ))}
                        </select>
                    </label>
                )}
                <ConnectionMenu />
                <AuthMenu />
                <LeaseMenu />
                <NodeMenu />
                <Button
                    size="xs"
                    variant={threeEnabled ? "primary" : "ghost"}
                    title={threeEnabled ? "关闭 Three.js 3D 渲染" : "开启 Three.js 3D 渲染（默认关闭）"}
                    onClick={() => setThreeEnabled(!threeEnabled)}
                >
                    ◇ 3D {threeEnabled ? "开" : "关"}
                </Button>
                <Button
                    size="xs"
                    variant="ghost"
                    onClick={toggle}
                    title={theme === "dark" ? "当前深色，点击切换到浅色" : "当前浅色，点击切换到深色"}
                >
                    {theme === "dark" ? "☾ 深色" : "☀ 浅色"}
                </Button>
                <Button
                    size="xs"
                    variant="danger"
                    disabled={!connected || !hasLease}
                    title={
                        !connected
                            ? "未连接"
                            : !hasLease
                            ? "须先持有租约（robot/estop）"
                            : "全局急停 robot/estop（快捷键 Esc）"
                    }
                    onClick={estop}
                >
                    ■ 急停
                </Button>
            </div>
        </header>
    );
}
