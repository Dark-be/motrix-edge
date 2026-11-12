import type { ReactNode } from "react";

/** 表单输入框（顶栏下拉 / 页面表单共用；窄栏也放得下）。 */
export const inputCls =
    "w-full min-w-0 rounded border border-line2 bg-panel2 px-2 py-1 font-mono text-[11px] text-ink placeholder-faint focus:border-accent focus:outline-none disabled:opacity-50";

/** 窄栏表单字段（label 在上、控件在下）。 */
export function FormField({ label, children, hint }: { label: string; children: ReactNode; hint?: ReactNode }) {
    return (
        <label className="flex min-w-0 flex-col gap-0.5">
            <span className="truncate text-[10px] uppercase tracking-wide text-faint" title={label}>
                {label}
            </span>
            {children}
            {hint && <span className="text-[10px] text-faint">{hint}</span>}
        </label>
    );
}

export type ChipTone = "slate" | "green" | "amber" | "red" | "blue";

const dotTone: Record<ChipTone, string> = {
    slate: "bg-faint",
    green: "bg-emerald-500",
    amber: "bg-amber-500",
    red: "bg-rose-500",
    blue: "bg-sky-500",
};

/** 顶栏状态小徽标（点 + 文本 + 可选值）；收起态下这就是唯一可见信息。 */
export function Chip({
    tone = "slate",
    label,
    value,
    title,
}: {
    tone?: ChipTone;
    label: string;
    value?: string;
    title?: string;
}) {
    return (
        <span
            title={title}
            className="inline-flex shrink-0 items-center gap-1.5 rounded border border-line bg-panel2 px-1.5 py-0.5"
        >
            <i className={`h-1.5 w-1.5 rounded-full ${dotTone[tone]}`} />
            <span className="text-[10px] uppercase tracking-wide text-faint">{label}</span>
            {value && <span className="tnum font-mono text-[11px] leading-4 text-ink">{value}</span>}
        </span>
    );
}

/** 顶栏遥测项（大写标签 + 等宽值）。 */
export function Stat({ label, value, tone = "" }: { label: string; value: string; tone?: string }) {
    return (
        <span className="inline-flex shrink-0 items-baseline gap-1">
            <span className="text-[10px] uppercase tracking-wide text-faint">{label}</span>
            <span className={`tnum font-mono text-[11px] ${tone || "text-ink"}`}>{value}</span>
        </span>
    );
}
