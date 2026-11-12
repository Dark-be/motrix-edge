import type { Toast } from "../hooks/useToasts";

/** 右下角 toast 栈：操作结果即时可见（日志抽屉仍保留完整历史）。 */
export function Toasts({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
    if (toasts.length === 0) return null;
    return (
        <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
            {toasts.map((t) => (
                <button
                    key={t.id}
                    type="button"
                    onClick={() => onDismiss(t.id)}
                    title="点击关闭"
                    className={`pointer-events-auto rounded-md border px-3 py-2 text-left text-xs shadow-lg backdrop-blur ${
                        t.ok
                            ? "border-emerald-700/60 bg-emerald-950/80 text-emerald-200"
                            : "border-rose-700/60 bg-rose-950/80 text-rose-200"
                    }`}
                >
                    <span className="mr-1 font-mono">{t.ok ? "✓" : "✕"}</span>
                    <span className="break-all">{t.msg}</span>
                </button>
            ))}
        </div>
    );
}
