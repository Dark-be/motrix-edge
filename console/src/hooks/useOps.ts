import { useCallback, useState } from "react";

import { brief } from "../components/ui";
import { errMsg } from "../types";

/**
 * 受控操作的公共状态：`busy` / `error` / 最近回执 + 统一的 `run` 包装。
 *
 * 会话卡、采集 / 推理操作、RTC、上传…此前各写一份同构的 `run`（改一处漏一处、文案也漂移），
 * 收敛到这里：
 *
 * - 成功 → 记 `lastResult` + 进日志（`brief` 压成一行）；
 * - 失败 → 记 `error` + 进日志（**不吞原因**）；
 * - **不向上抛**：调用方一律 `void run(...)`，避免未处理的 rejection（按钮的语义是「派发动作」，
 *   结果通过 error / 日志呈现）。
 */
export function useOps(onLog: (msg: string, ok: boolean) => void) {
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [lastResult, setLastResult] = useState<string | null>(null);

    const run = useCallback(
        async (label: string, fn: () => Promise<unknown>): Promise<void> => {
            setBusy(true);
            setError(null);
            try {
                const res = await fn();
                setLastResult(`${label} → ${JSON.stringify(res)}`);
                onLog(`${label} 成功: ${brief(res)}`, true);
            } catch (e) {
                const msg = errMsg(e);
                setError(msg);
                setLastResult(`${label} 失败`);
                onLog(`${label} 失败: ${msg}`, false);
            } finally {
                setBusy(false);
            }
        },
        [onLog]
    );

    const clear = useCallback(() => {
        setError(null);
        setLastResult(null);
    }, []);

    return { busy, error, lastResult, run, setError, clear };
}
