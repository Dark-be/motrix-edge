export interface Signal {
    label: string;
    /** 底层命令名（展示在按钮 title，便于对照 CLI） */
    command: string;
    /** HTTP 端点（展示在按钮 title，便于对照文档 / curl） */
    endpoint: string;
    variant: "primary" | "danger" | "default";
    enabled: boolean;
    /** 不能操作的原因（按钮 title 里说清楚，而不是默默置灰） */
    whyDisabled?: string;
    run: () => Promise<unknown>;
}

const variantCls: Record<Signal["variant"], string> = {
    danger: "border-rose-800/70 bg-rose-950/30 hover:bg-rose-900/40",
    primary: "border-sky-700/70 bg-sky-950/30 hover:bg-sky-900/40",
    default: "border-slate-800 bg-slate-950/40 hover:border-slate-600",
};

/**
 * 信号按钮网格：「一个按钮 = 一个命令」。
 *
 * 按钮只留**一行命令名**（端点 / 命令 / 置灰原因全部进 title）：副标题那行
 * `POST /v1/commands (capability=…)` 在主区 1/2 宽度下必被截断，只赚噪声不赚信息。
 * 采集 / 推理 / 机器人 / 上传四处共用同一套样式与「为什么不能点」的提示逻辑。
 */
export function SignalGrid({
    signals,
    busy,
    onRun,
}: {
    signals: Signal[];
    busy: boolean;
    onRun: (label: string, run: () => Promise<unknown>) => void;
}) {
    return (
        <div className="grid grid-cols-2 gap-1.5 lg:grid-cols-3">
            {signals.map((s) => (
                <button
                    key={s.label}
                    type="button"
                    disabled={!s.enabled || busy}
                    onClick={() => onRun(s.label, s.run)}
                    className={`truncate rounded-md border px-2.5 py-1.5 text-left text-xs font-semibold text-slate-100 transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                        variantCls[s.variant]
                    }`}
                    title={`${s.label}\n${s.endpoint}\n命令: ${s.command}${
                        s.enabled ? "" : `\n不可用：${s.whyDisabled ?? "—"}`
                    }`}
                >
                    {s.label}
                </button>
            ))}
        </div>
    );
}
