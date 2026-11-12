import type { ReactNode } from "react";

import type { ChipTone } from "./controls";

const badgeTone: Record<ChipTone, string> = {
    slate: "bg-elev text-muted",
    green: "bg-emerald-500/15 text-emerald-600 ring-1 ring-inset ring-emerald-500/30",
    amber: "bg-amber-500/15 text-amber-600 ring-1 ring-inset ring-amber-500/30",
    red: "bg-rose-500/15 text-rose-600 ring-1 ring-inset ring-rose-500/30",
    blue: "bg-sky-500/15 text-sky-600 ring-1 ring-inset ring-sky-500/30",
};

/**
 * 页面级状态条（标签栏下方一行）：模式徽标 + 实时状态 + 关键计数 / 操作按钮。
 *
 * 只放**状态与操作**，不放操作说明（说明写在文档里）——界面留给数据与控件。
 */
export function StatusStrip({
    mode,
    tone = "blue",
    live,
    stage,
    actions,
}: {
    mode: string;
    tone?: ChipTone;
    /** 与真机的实时状态（预览 / 租约就绪） */
    live?: { ok: boolean; label: string };
    /** 关键计数 / 阶段（如 `STEP 12`、`REC · 3 轮`） */
    stage?: string;
    actions?: ReactNode;
}) {
    return (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line bg-panel2 px-3 py-1 text-[11px]">
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide ${badgeTone[tone]}`}>
                {mode}
            </span>
            {live && (
                <span
                    className={`flex items-center gap-1 text-[10px] ${live.ok ? "text-emerald-600" : "text-amber-600"}`}
                >
                    <i className={`h-1.5 w-1.5 rounded-full ${live.ok ? "bg-emerald-500" : "bg-amber-500"}`} />
                    {live.label}
                </span>
            )}
            <div className="ml-auto flex items-center gap-2">
                {stage && <span className="tnum font-mono text-[10px] text-faint">{stage}</span>}
                {actions}
            </div>
        </div>
    );
}
