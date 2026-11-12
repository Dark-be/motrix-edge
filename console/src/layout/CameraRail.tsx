import { useEffect, useRef } from "react";

import { useEdge } from "../edge";
import { useWebRTC } from "../hooks/useWebRTC";
import { Button, Card } from "../components/ui";

/**
 * 右栏相机监视器：WebRTC（`POST /v1/webrtc/offer`）逐相机一路 recvonly 视频轨，竖向堆叠。
 *
 * 图像不内联（HTTP JSON 不带二进制）；相机名来自 `/v1/preview` 的 `images`。持租约 +
 * 有相机时**自动连接**，相机集合变化自动重连。
 */
export function CameraRail() {
    const { api, cameraNames, hasLease } = useEdge();
    const cameraKey = cameraNames.join(",");
    const { state, error, streams, connect, disconnect } = useWebRTC(api, cameraNames);

    useEffect(() => {
        if (hasLease && cameraNames.length > 0) void connect();
        else disconnect();
    }, [hasLease, cameraKey, cameraNames.length, connect, disconnect]);

    // @types/react 的 video 无 srcObject prop：用 ref 回调挂流（只在流真正变化时设置）
    const videoEls = useRef<(HTMLVideoElement | null)[]>([]);
    useEffect(() => {
        streams.forEach((stream, i) => {
            const el = videoEls.current[i];
            if (el && stream) el.srcObject = stream;
        });
    }, [streams]);

    const live = state === "connected";
    const connecting = state === "connecting" || state === "checking" || state === "negotiating";

    return (
        <Card
            title="Camera"
            actions={
                <>
                    <span
                        className={`flex items-center gap-1 text-[10px] ${
                            live ? "text-emerald-600" : connecting ? "text-amber-600" : "text-faint"
                        }`}
                    >
                        <i
                            className={`h-1.5 w-1.5 rounded-full ${
                                live ? "bg-emerald-500" : connecting ? "bg-amber-500" : "bg-faint"
                            }`}
                        />
                        {cameraNames.length} 路 · {state}
                    </span>
                    {live ? (
                        <Button size="xs" variant="danger" onClick={disconnect}>
                            断开
                        </Button>
                    ) : (
                        <Button
                            size="xs"
                            variant="primary"
                            disabled={!hasLease || cameraNames.length === 0}
                            title={!hasLease ? "需先持有租约" : "重新协商视频流"}
                            onClick={() => void connect()}
                        >
                            连接
                        </Button>
                    )}
                </>
            }
        >
            {!hasLease ? (
                <p className="p-1 text-[11px] text-muted">需租约</p>
            ) : cameraNames.length === 0 ? (
                <p className="p-1 text-[11px] text-muted">无相机</p>
            ) : (
                <div className="flex flex-col gap-2">
                    {cameraNames.map((name, i) => (
                        <figure key={name} className="overflow-hidden rounded border border-line">
                            <div className="relative bg-black">
                                <video
                                    ref={(el) => {
                                        videoEls.current[i] = el;
                                    }}
                                    autoPlay
                                    muted
                                    playsInline
                                    className="aspect-video w-full object-contain"
                                />
                                {!live && (
                                    <div className="absolute inset-0 flex items-center justify-center text-[11px] text-slate-400">
                                        {error ? "连接失败" : connecting ? `协商中…（${state}）` : "未连接"}
                                    </div>
                                )}
                            </div>
                            <figcaption
                                className="truncate border-t border-line bg-panel2 px-2 py-0.5 font-mono text-[10px] text-muted"
                                title={name}
                            >
                                {name}
                            </figcaption>
                        </figure>
                    ))}
                </div>
            )}
            {error && <p className="mt-1 text-[11px] text-rose-400">{error}</p>}
        </Card>
    );
}
