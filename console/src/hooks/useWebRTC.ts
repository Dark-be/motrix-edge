import { useCallback, useEffect, useRef, useState } from "react";

import type { EdgeApi } from "../api";
import { errMsg } from "../types";

export type RtcState =
    | "idle"
    | "negotiating"
    | "new"
    | "connecting"
    | "checking"
    | "connected"
    | "disconnected"
    | "failed"
    | "closed";

/**
 * WebRTC **多相机 video track** hook：为每路相机建一个 recvonly 视频轨（transceiver 顺序 =
 * 相机顺序）→ offer → POST /v1/webrtc/offer → answer → 每路 `<video>` 播放。
 * 必须发送 setLocalDescription 后的 localDescription.sdp（含 ICE 候选），否则连接卡 checking。
 */
export function useWebRTC(api: EdgeApi | null, cameraNames: string[]) {
    const pcRef = useRef<RTCPeerConnection | null>(null);
    // 相机列表随 preview 轮询刷新（新引用），用 ref 持有最新值，避免 connect 每次重建导致重连
    const cameraNamesRef = useRef(cameraNames);
    cameraNamesRef.current = cameraNames;
    const [state, setState] = useState<RtcState>("idle");
    const [error, setError] = useState<string | null>(null);
    // 每路相机的 MediaStream（index = 相机顺序）
    const [streams, setStreams] = useState<(MediaStream | null)[]>([]);

    const disconnect = useCallback(() => {
        const pc = pcRef.current;
        pcRef.current = null;
        if (pc) {
            pc.onconnectionstatechange = null;
            pc.ontrack = null;
            pc.close();
        }
        setStreams([]);
        setState("idle");
        setError(null);
    }, []);

    const connect = useCallback(async () => {
        if (!api) return;
        const count = cameraNamesRef.current.length;
        if (count === 0) return;
        disconnect();
        try {
            setState("negotiating");
            const pc = new RTCPeerConnection();
            pcRef.current = pc;
            const tracks: (MediaStream | null)[] = new Array(count).fill(null);
            // 每相机一路 recvonly 视频轨（顺序 = 相机顺序，ontrack 按序匹配）
            for (let i = 0; i < count; i++) {
                pc.addTransceiver("video", { direction: "recvonly" });
            }
            let idx = 0;
            pc.ontrack = (e) => {
                if (idx < count) {
                    tracks[idx] = e.streams[0];
                    setStreams([...tracks]);
                    idx += 1;
                }
            };
            pc.onconnectionstatechange = () => {
                const st = pc.connectionState as RtcState;
                setState(st);
                if (st === "failed" || st === "closed") {
                    setError(`connection ${st}`);
                } else if (st === "connected") {
                    setError(null);
                }
            };

            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);
            if (!pc.localDescription) throw new Error("localDescription unavailable");
            const answer = await api.webrtcOffer(pc.localDescription.sdp, pc.localDescription.type);
            await pc.setRemoteDescription(
                new RTCSessionDescription({ type: answer.type as RTCSdpType, sdp: answer.sdp })
            );
        } catch (e) {
            setError(errMsg(e));
            setState("failed");
            pcRef.current?.close();
            pcRef.current = null;
        }
    }, [api, disconnect]);

    useEffect(() => disconnect, [disconnect]);

    return { state, error, streams, connect, disconnect };
}
