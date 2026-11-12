import { useEffect, useState } from "react";

/** 秒级计时（顶部 `t+`）：从 `from`（连接时刻）起算，未连接返回 `null`。 */
export function useElapsed(from: number | null): number | null {
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        if (!from) return;
        const id = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(id);
    }, [from]);
    if (!from) return null;
    return Math.max(0, Math.floor((now - from) / 1000));
}
