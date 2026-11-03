import { useCallback, useEffect, useRef, useState } from "react";

import { errMsg } from "../types";

/**
 * 通用轮询 hook：enabled 时每 intervalMs 调用一次 fn，返回最新数据 / 错误。
 *
 * 三条保护（都是「别让轮询把界面和 Edge 拖垮」）：
 * - **不叠加**：上一发没返回就跳过本轮（Edge handler 是同步阻塞的，慢请求叠起来只会更慢）；
 * - **不可见不轮询**：标签页在后台时不发请求，切回来立刻补一次；
 * - **保留上次好数据**：请求失败时不清空 `data`（界面不闪空），错误单独暴露给调用方展示。
 */
export function usePolling<T>(fn: () => Promise<T>, intervalMs: number, enabled: boolean) {
    const [data, setData] = useState<T | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [lastOkAt, setLastOkAt] = useState<number | null>(null);
    const fnRef = useRef(fn);
    fnRef.current = fn;
    const inFlight = useRef(false);

    const tick = useCallback(async () => {
        if (inFlight.current || document.hidden) return; // 未返回 / 页面不可见 → 跳过本轮
        inFlight.current = true;
        try {
            const d = await fnRef.current();
            setData(d);
            setError(null);
            setLastOkAt(Date.now());
        } catch (e) {
            setError(errMsg(e));
        } finally {
            inFlight.current = false;
        }
    }, []);

    useEffect(() => {
        if (!enabled) return;
        let cancelled = false;
        const run = () => {
            if (!cancelled) void tick();
        };
        run();
        const id = window.setInterval(run, intervalMs);
        const onVisible = () => {
            if (!document.hidden) run();
        };
        document.addEventListener("visibilitychange", onVisible);
        return () => {
            cancelled = true;
            window.clearInterval(id);
            document.removeEventListener("visibilitychange", onVisible);
        };
    }, [enabled, intervalMs, tick]);

    // 停止轮询（enabled=false）时清除陈旧错误（如租约过期 410 残留），避免卡片残留报错：
    // 观测卡片因无租约停止轮询后，不应继续显示历史 410，而是回到待机文案。
    useEffect(() => {
        if (!enabled) setError(null);
    }, [enabled]);

    return { data, error, lastOkAt };
}
