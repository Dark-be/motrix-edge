import { useEffect, useRef, useState, type ReactNode } from "react";

/**
 * 顶栏下拉（租约 / 节点 / 连接）：收起时只显示一行状态（`label`），点开才展开细节。
 *
 * Esc 关闭时**吃掉**按键（捕获阶段 stopPropagation），避免与全局急停快捷键冲突。
 */
export function Dropdown({
    label,
    children,
    panelClass = "w-80",
    title,
}: {
    label: ReactNode;
    children: ReactNode;
    /** 展开面板宽度（Tailwind 类，如 `w-80` / `w-[22rem]`） */
    panelClass?: string;
    title?: string;
}) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        if (!open) return;
        const onPointerDown = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
        };
        const onKeyDown = (e: KeyboardEvent) => {
            if (e.key !== "Escape") return;
            e.stopPropagation();
            e.preventDefault();
            setOpen(false);
        };
        document.addEventListener("mousedown", onPointerDown);
        // 捕获阶段：先于全局急停（window 冒泡）处理 Esc
        document.addEventListener("keydown", onKeyDown, true);
        return () => {
            document.removeEventListener("mousedown", onPointerDown);
            document.removeEventListener("keydown", onKeyDown, true);
        };
    }, [open]);

    return (
        <div className="relative" ref={ref}>
            <button
                type="button"
                title={title}
                aria-expanded={open}
                onClick={() => setOpen(!open)}
                className={`flex items-center gap-1 rounded border px-1.5 py-0.5 transition-colors ${
                    open ? "border-accent bg-panel2" : "border-line bg-panel2 hover:border-line2"
                }`}
            >
                {label}
                <span className={`text-[8px] text-faint transition-transform ${open ? "rotate-180" : ""}`}>▼</span>
            </button>
            {open && (
                <div
                    className={`absolute right-0 z-40 mt-1 max-h-[70vh] overflow-y-auto rounded-lg border border-line bg-panel p-2.5 shadow-xl ${panelClass}`}
                >
                    {children}
                </div>
            )}
        </div>
    );
}
