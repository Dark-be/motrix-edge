import { createContext, useContext, useEffect, type ReactNode } from "react";

import { useEdge } from "./edge";
import { useWebRTC } from "./hooks/useWebRTC";

type WebRTCState = ReturnType<typeof useWebRTC>;

const WebRTCCtx = createContext<WebRTCState | null>(null);

/**
 * Console 级 WebRTC 生命周期：页面标签只展示媒体流，不拥有 PeerConnection。
 * 标签切换不会重新协商；租约、Edge 目标或相机集合变化时才重连。
 */
export function WebRTCProvider({ children }: { children: ReactNode }) {
    const { api, cameraNames, hasLease } = useEdge();
    const cameraKey = cameraNames.join(",");
    const rtc = useWebRTC(api, cameraNames);

    useEffect(() => {
        if (hasLease && cameraNames.length > 0) void rtc.connect();
        else rtc.disconnect();
    }, [api, hasLease, cameraKey, cameraNames.length, rtc.connect, rtc.disconnect]);

    return <WebRTCCtx.Provider value={rtc}>{children}</WebRTCCtx.Provider>;
}

export function useConsoleWebRTC(): WebRTCState {
    const value = useContext(WebRTCCtx);
    if (!value) throw new Error("useConsoleWebRTC 必须在 <WebRTCProvider> 内使用");
    return value;
}
