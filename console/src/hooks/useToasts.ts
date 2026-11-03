import { useCallback, useRef, useState } from "react";

export interface Toast {
    id: number;
    msg: string;
    ok: boolean;
}

/** toast 存活时间（毫秒）：够看清一行结果，又不挡操作 */
const TTL_MS = 4500;
/** 同屏最多保留的条数（新的顶掉最旧的） */
const MAX_TOASTS = 4;

/** 轻量 toast 栈：受控操作的结果在右下角弹一条并自动消失（不打断操作，也不刷屏）。 */
export function useToasts() {
    const [toasts, setToasts] = useState<Toast[]>([]);
    const seq = useRef(0);

    const push = useCallback((msg: string, ok: boolean) => {
        seq.current += 1;
        const id = seq.current;
        setToasts((prev) => [...prev, { id, msg, ok }].slice(-MAX_TOASTS));
        window.setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), TTL_MS);
    }, []);

    const dismiss = useCallback((id: number) => setToasts((prev) => prev.filter((t) => t.id !== id)), []);

    return { toasts, push, dismiss };
}
