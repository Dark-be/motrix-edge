import { useCallback, useEffect, useState } from "react";

import { useEdge } from "../edge";
import { errMsg, type AdapterDetails, type AdapterEffectiveConfig, type AdaptersResponse } from "../types";

export interface AdapterInfo {
    /** 当前绑定 adapter 的完整能力（`GET /v1/adapters` 里按 type 匹配） */
    details: AdapterDetails | null;
    /** 当前实际生效的能力配置（启用臂 / 相机 / 各动作空间维度 / home） */
    current: AdapterEffectiveConfig | null;
    error: string | null;
    refresh: () => Promise<void>;
}

/**
 * 适配器能力信息（`GET /v1/adapters` + `GET /v1/adapters/current`）。
 *
 * 节点状态变化时自动重取（换 adapter / 复位后能力可能变），也可手动 `refresh()`——
 * 手动页 / 调试页共用，避免各自轮询同一份只读数据。
 */
export function useAdapterInfo(): AdapterInfo {
    const { api, connected, nodeState } = useEdge();
    const [list, setList] = useState<AdaptersResponse | null>(null);
    const [current, setCurrent] = useState<AdapterEffectiveConfig | null>(null);
    const [error, setError] = useState<string | null>(null);

    const refresh = useCallback(async () => {
        if (!api) return;
        try {
            const [adapters, effective] = await Promise.all([api.adapters(), api.adapterCurrent()]);
            setList(adapters);
            setCurrent(effective);
            setError(null);
        } catch (e) {
            setError(errMsg(e));
        }
    }, [api]);

    useEffect(() => {
        if (!connected) {
            setList(null);
            setCurrent(null);
            return;
        }
        void refresh();
    }, [connected, nodeState, refresh]);

    const type = current?.adapter.type ?? null;
    const details =
        list?.adapters.find((a) => a.type === type) ?? (list?.adapters.length === 1 ? list.adapters[0] : null);

    return { details, current, error, refresh };
}
