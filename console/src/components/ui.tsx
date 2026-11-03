import { useState, type ReactNode } from "react";

/** 深色控制台风格的基础 UI 组件（Card / Badge / Button / Field）。 */

export type Tone = "slate" | "green" | "red" | "amber" | "blue";

const toneClasses: Record<Tone, string> = {
    slate: "bg-slate-700/60 text-slate-200",
    green: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
    red: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
    amber: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
    blue: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
};

/**
 * 数值 / 标识符文本：**单行截断**（悬浮看全值），空值统一显示 `—`。
 *
 * 不用 `break-all`：窄栏（如右栏 1/3 宽）逐字断行会把 `edge-test-001` 拆成
 * `edge-te` / `st-001`，可读性比截断更差；长 id / 路径一律截断 + 悬浮。
 */
export function Text({ children, className = "" }: { children?: ReactNode; className?: string }) {
    if (children === null || children === undefined || children === "") {
        return <span className="text-slate-600">—</span>;
    }
    if (typeof children === "string" || typeof children === "number") {
        const text = String(children);
        return (
            // `block`：inline 元素上的 overflow / text-overflow 不生效（窄栏长路径会顶破容器）
            <span className={`block truncate ${className}`} title={text}>
                {text}
            </span>
        );
    }
    return <span className={`inline-flex min-w-0 items-center ${className}`}>{children}</span>;
}

/** 去掉后端 help 文本里的 markdown 标记（`**` / 反引号），避免在界面上原样显示。 */
export function plain(text: string | null | undefined): string {
    if (!text) return "";
    return text.replace(/\*\*/g, "").replace(/`/g, "");
}

/**
 * meta 值展示：纯浮点数保留 3 位小数（机器人进程会写全精度 `duration`），其余原样。
 *
 * 值可能是 number（如 `frames` / `duration`）、字符串，也可能是**结构化自描述**——机器人进程在
 * `capture_status.meta` 里带 `state_space` / `state_dims` / `action_space` / `action_dims`
 * （后两者是逐维 `{index, arm, kind, name}` 的数组，14 项逐条铺开会把面板撑爆）→ 这里只给
 * **维度数量**，逐维明细走 :func:`fmtMetaDetail`（tooltip）。直接 `String(value)` 只会得到
 * `[object Object],[object Object]`。
 */
export function fmtMetaValue(value: unknown): string {
    if (value === null || value === undefined) return "";
    if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(3);
    if (isDimArray(value)) return String(value.length); // 逐维含义：只给维度数量（明细在 tooltip）
    if (Array.isArray(value)) return value.map(fmtMetaItem).join(" · ");
    if (typeof value === "object") return JSON.stringify(value);
    const text = String(value);
    const n = Number(text);
    if (text.trim() !== "" && Number.isFinite(n) && !Number.isInteger(n)) return n.toFixed(3);
    return text;
}

/** meta 值明细（tooltip 用）：逐维含义展开成 `left/joint/j1 · …`，其余同 `fmtMetaValue`。 */
export function fmtMetaDetail(value: unknown): string {
    if (isDimArray(value)) return value.map(fmtMetaItem).join(" · ");
    return fmtMetaValue(value);
}

/** 逐维含义数组（`state_dims` / `action_dims`：每项 `{index, arm, kind, name}`）。 */
function isDimArray(value: unknown): value is Record<string, unknown>[] {
    return (
        Array.isArray(value) &&
        value.length > 0 &&
        value.every((item) => item !== null && typeof item === "object" && "index" in item && "kind" in item)
    );
}

/** 数组项：逐维含义 `{index, arm, kind, name}` → `left/joint/j1`；其余项按标量 / JSON 展示。 */
function fmtMetaItem(item: unknown): string {
    if (item !== null && typeof item === "object") {
        const { arm, kind, name, index } = item as Record<string, unknown>;
        if (kind !== undefined || name !== undefined) {
            return [arm, kind, name ?? index].filter((part) => part !== undefined && part !== "").join("/");
        }
        return JSON.stringify(item);
    }
    return fmtMetaValue(item);
}

export function Badge({ tone = "slate", children }: { tone?: Tone; children: ReactNode }) {
    return (
        <span
            className={`inline-flex shrink-0 items-center rounded px-1.5 py-px text-[10px] font-medium leading-4 ring-1 ring-inset ${toneClasses[tone]}`}
        >
            {children}
        </span>
    );
}

type ButtonVariant = "primary" | "danger" | "default" | "ghost";

const btnVariants: Record<ButtonVariant, string> = {
    primary: "bg-sky-600 hover:bg-sky-500 text-white shadow-sm disabled:bg-sky-900 disabled:text-sky-400",
    danger: "bg-rose-600 hover:bg-rose-500 text-white shadow-sm disabled:bg-rose-900 disabled:text-rose-400",
    default: "bg-slate-700 hover:bg-slate-600 text-slate-100 disabled:bg-slate-800 disabled:text-slate-500",
    ghost: "bg-transparent hover:bg-slate-800 text-slate-300 border border-slate-700 disabled:text-slate-600",
};

const btnSizes = {
    sm: "px-2.5 py-1 text-xs",
    xs: "px-2 py-0.5 text-[11px]",
} as const;

export function Button({
    variant = "default",
    size = "sm",
    type = "button",
    onClick,
    disabled,
    children,
    title,
    className = "",
}: {
    variant?: ButtonVariant;
    /** sm（默认）/ xs（卡片头、表格工具条等紧凑位置） */
    size?: keyof typeof btnSizes;
    type?: "button" | "submit";
    onClick?: () => void;
    disabled?: boolean;
    children: ReactNode;
    title?: string;
    className?: string;
}) {
    return (
        <button
            type={type}
            onClick={onClick}
            disabled={disabled}
            title={title}
            className={`inline-flex shrink-0 items-center justify-center gap-1 whitespace-nowrap rounded-md font-medium transition-colors disabled:cursor-not-allowed ${btnSizes[size]} ${btnVariants[variant]} ${className}`}
        >
            {children}
        </button>
    );
}

export function Card({
    title,
    subtitle,
    actions,
    children,
    className = "",
    collapsible = false,
    collapsed = false,
    onToggle,
}: {
    title: ReactNode;
    /** 单行 API 参考（如 `POST /v1/captures`）：标题同行右贴，超长截断 */
    subtitle?: ReactNode;
    actions?: ReactNode;
    children: ReactNode;
    className?: string;
    /** 可在头部折叠 / 展开（收起暂时不用的卡片） */
    collapsible?: boolean;
    collapsed?: boolean;
    onToggle?: () => void;
}) {
    return (
        <section className={`rounded-lg border border-slate-800 bg-slate-900/60 ${className}`}>
            <header className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-slate-800 px-3 py-1.5">
                {collapsible && onToggle && (
                    <button
                        type="button"
                        onClick={onToggle}
                        title={collapsed ? "展开" : "收起"}
                        aria-expanded={!collapsed}
                        className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-slate-500 transition-colors hover:bg-slate-800 hover:text-slate-100"
                    >
                        <span
                            className={`inline-block text-[9px] transition-transform ${collapsed ? "" : "rotate-90"}`}
                        >
                            ▶
                        </span>
                    </button>
                )}
                <div className="flex min-w-0 flex-1 items-baseline gap-2">
                    <h2 className="shrink-0 text-[13px] font-semibold text-slate-100">{title}</h2>
                    {subtitle && (
                        <p
                            className="min-w-0 truncate font-mono text-[10px] text-slate-500"
                            title={typeof subtitle === "string" ? subtitle : undefined}
                        >
                            {subtitle}
                        </p>
                    )}
                </div>
                {actions && <div className="flex flex-wrap items-center gap-1.5">{actions}</div>}
            </header>
            {!collapsed && <div className="p-2.5">{children}</div>}
        </section>
    );
}

/** 卡内小标题（区块 / 分组）。 */
export function BlockTitle({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
    return (
        <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1">
            <h3 className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">{children}</h3>
            {actions && <div className="ml-auto flex flex-wrap items-center gap-1.5">{actions}</div>}
        </div>
    );
}

/**
 * 卡内可折叠区块（比 :func:`Card` 轻：无第二层卡片边框）。
 *
 * 用途：把低频 / 诊断类内容（策略服务器 metadata、推理参数、上传队列细节）从主区收进去，
 * 主区只留状态与常用操作——**默认收起**，`actions` 里的徽标仍可见（收起不丢状态）。
 */
export function Disclosure({
    label,
    actions,
    defaultOpen = false,
    children,
}: {
    label: ReactNode;
    actions?: ReactNode;
    defaultOpen?: boolean;
    children: ReactNode;
}) {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <div className="rounded border border-slate-800 bg-slate-950/40">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-2 py-1">
                <button
                    type="button"
                    aria-expanded={open}
                    onClick={() => setOpen(!open)}
                    className="flex min-w-0 items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500 transition-colors hover:text-slate-200"
                >
                    <span className={`inline-block text-[9px] transition-transform ${open ? "rotate-90" : ""}`}>▶</span>
                    <span className="truncate">{label}</span>
                </button>
                {actions && <div className="ml-auto flex flex-wrap items-center gap-1.5">{actions}</div>}
            </div>
            {open && <div className="border-t border-slate-800 p-2">{children}</div>}
        </div>
    );
}

/**
 * 分段选择（互斥一组）。
 *
 * 比一排按钮更省地方、状态更清楚（选中项高亮 + `aria-pressed`）；`disabled` 项带 `title`
 * 说明原因（不默默置灰）。
 */
export function Segmented<T extends string>({
    value,
    options,
    onChange,
    disabled = false,
}: {
    value: T;
    options: { value: T; label: string; title?: string; disabled?: boolean }[];
    onChange: (value: T) => void;
    disabled?: boolean;
}) {
    return (
        <div className="inline-flex max-w-full shrink-0 flex-wrap items-center gap-0.5 rounded-md border border-slate-800 bg-slate-950/60 p-0.5">
            {options.map((option) => {
                const active = option.value === value;
                const off = disabled || option.disabled === true;
                return (
                    <button
                        key={option.value}
                        type="button"
                        aria-pressed={active}
                        disabled={off}
                        title={option.title}
                        onClick={() => onChange(option.value)}
                        className={`rounded px-2.5 py-1 text-[11px] font-medium transition-colors ${
                            active ? "bg-sky-600 text-white" : "text-slate-300 hover:bg-slate-800"
                        } disabled:cursor-not-allowed disabled:opacity-40`}
                    >
                        {option.label}
                    </button>
                );
            })}
        </div>
    );
}

/** 一行一个字段：label 左、值右，值单行截断（长 id / 路径悬浮看全）。 */
export function KeyValue({ k, v }: { k: string; v?: ReactNode }) {
    return (
        <div className="flex items-baseline gap-3 py-[2px]">
            <span className="min-w-0 max-w-[60%] shrink-0 truncate text-[11px] text-slate-500" title={k}>
                {k}
            </span>
            <span className="ml-auto min-w-0 text-right font-mono text-[11px] text-slate-200">
                <Text>{v}</Text>
            </span>
        </div>
    );
}

export function KvGrid({ children }: { children: ReactNode }) {
    return <dl>{children}</dl>;
}

/** 窄栏紧凑字段：label 在上、值在下（值单行截断），比「label 左 + 值右」更耐窄列。 */
export function Field({ k, v, wide = false }: { k: string; v?: ReactNode; wide?: boolean }) {
    return (
        <div className={`min-w-0 ${wide ? "col-span-full" : ""}`}>
            <div className="truncate text-[10px] uppercase tracking-wide text-slate-500" title={k}>
                {k}
            </div>
            <div className="min-w-0 font-mono text-[11px] text-slate-200">
                <Text>{v}</Text>
            </div>
        </div>
    );
}

/** 字段网格（默认两列；窄栏下也不会把值挤成逐字断行）。 */
export function FieldGrid({ cols = 2, children }: { cols?: 1 | 2 | 3; children: ReactNode }) {
    const cls = cols === 1 ? "grid-cols-1" : cols === 3 ? "sm:grid-cols-3" : "grid-cols-2";
    return <div className={`grid ${cls} gap-x-3 gap-y-1.5`}>{children}</div>;
}

/** 开关（卡片头的小尺寸开关；文本不参与点击，避免与按钮重复触发）。 */
export function Switch({
    checked,
    onChange,
    label,
    title,
    disabled = false,
}: {
    checked: boolean;
    onChange: (next: boolean) => void;
    label: string;
    title?: string;
    /** 置灰不可点（如无租约 / 节点未就绪）：`title` 说明原因 */
    disabled?: boolean;
}) {
    return (
        <span
            className={`flex shrink-0 items-center gap-1.5 text-[11px] text-slate-300 ${disabled ? "opacity-50" : ""}`}
            title={title}
        >
            <button
                type="button"
                role="switch"
                aria-checked={checked}
                disabled={disabled}
                onClick={() => onChange(!checked)}
                className={`relative h-4 w-7 shrink-0 rounded-full transition-colors disabled:cursor-not-allowed ${
                    checked ? "bg-sky-600" : "bg-slate-700"
                }`}
            >
                <span
                    className={`absolute top-0.5 h-3 w-3 rounded-full bg-white transition-all ${
                        checked ? "left-3.5" : "left-0.5"
                    }`}
                />
            </button>
            {label}
        </span>
    );
}

export function ErrorText({ message }: { message: string | null }) {
    if (!message) return null;
    return (
        <p className="mt-2 rounded border border-rose-800/60 bg-rose-950/40 px-2 py-1 font-mono text-[11px] leading-relaxed text-rose-300">
            {message}
        </p>
    );
}

/** 回执压成一行摘要（日志 / toast 用）。
 *
 * 完整 JSON 一律在面板内的 `lastResult` / 状态区展示——日志只留「谁做了什么、成没成」，
 * 否则一次上传 / 预检回执就能把日志与 toast 扑满。
 */
export function brief(value: unknown, max = 160): string {
    let text: string;
    try {
        text = typeof value === "string" ? value : JSON.stringify(value);
    } catch {
        text = String(value);
    }
    if (text === undefined) return "";
    return text.length > max ? `${text.slice(0, max)}…` : text;
}

export function fmtBytes(n?: number): string {
    if (n === undefined || Number.isNaN(n)) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = n;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
    }
    return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function fmtDuration(seconds: number | null): string {
    if (seconds === null || Number.isNaN(seconds)) return "—";
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(sec).padStart(2, "0");
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function fmtTime(iso: string | null | undefined): string {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString("zh-CN", { hour12: false });
}

export function truncateId(id: string | null | undefined, n = 12): string {
    if (!id) return "—";
    return id.length > n ? `${id.slice(0, n)}…` : id;
}
