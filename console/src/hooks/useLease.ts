import { useCallback, useEffect, useRef, useState } from "react";

import type { EdgeApi } from "../api";
import { errMsg, type LeaseInstallRequest, type LeaseStatus } from "../types";

/** 租约签发表单值（本页 Console 侧生成租约：部分字段 + 持续时间）。 */
export interface LeaseFormValues {
    lease_id: string;
    edge_id: string;
    holder_subject_id: string;
    purpose: string;
    state: "reserved" | "active";
    /** 持续时间（秒）—— 据此计算 expires_at。 */
    ttl: number;
}

/**
 * 租约管理 hook —— **每页独立持有**；`GET /v1/leases` 仅展示，不作为本页持有：
 *
 * - **Edge 状态仅供展示**：`GET /v1/leases`（连接后 + 每 5s 轮询）只反映「Edge 当前租约
 *   槽位」；**绝不把返回的 lease_id 当成本页持有**（否则新标签页会自动搭乘别人的租约）。
 * - **本页持有 = 本页自己 install 签发的租约**（仅内存态：每标签页独立、不跨页共享、
 *   刷新即失联）；受控请求 `X-Lease-Id` / 续约 / 撤销只针对本页签发的租约。
 * - 签发：install（表单 → POST /v1/leases，计算 expires_at，成为本页持有）
 * - 续约：renew（lease_version+1 + 新 expires_at → POST /v1/leases/{id}:renew）
 * - 撤销本页租约：revoke（POST /v1/leases/{id}:revoke）
 * - 撤销占用并接管：takeover（撤销**其它会话**的当前占用租约 —— 显式动作，绝不自动；
 *   供原持有页关闭 / 崩溃后接管）
 * - 自动续租：按 renew_interval 定时 renew（仅当本页持有活跃租约）
 */
export function useLease(api: EdgeApi | null) {
    const [status, setStatus] = useState<LeaseStatus | null>(null);
    const [countdown, setCountdown] = useState<number | null>(null);
    const [autoRenew, setAutoRenew] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [expiredNotice, setExpiredNotice] = useState<string | null>(null);
    // 本页签发的租约 id（**内存态**：每标签页独立、不跨页共享、刷新即失联）
    const [myLeaseId, setMyLeaseId] = useState<string | null>(null);
    const renewTimer = useRef<number | null>(null);

    // ---- Edge 当前租约（**仅展示**：state / lease_id 反映 Edge 槽位）----
    const edgeState = status?.state ?? null;
    const edgeLeaseId = status?.lease_id ?? null;
    // 本页签发的租约是否仍是 Edge 当前租约
    const isMine = myLeaseId !== null && myLeaseId === edgeLeaseId;
    // 本页持有且 Edge 上 active：受控操作 + 自动续租可用（App 的 hasLease）
    const holding = isMine && edgeState === "active";
    // 本页租约已过期（仍是当前租约，可续约重新激活）
    const expiredMine = isMine && edgeState === "expired";
    // Edge 被**其它会话 / 标签页**占用（active / reserved）：本页不可签发 / 操作，只能显式接管
    const occupiedByOther = edgeLeaseId !== null && (edgeState === "active" || edgeState === "reserved") && !isMine;
    // 本页可否签发自己的租约：Edge 无活跃占用（无租约 / 已撤销 / 已过期）
    const canIssue = edgeState !== "active" && edgeState !== "reserved";

    const refresh = useCallback(async () => {
        if (!api) return;
        try {
            const s = await api.leaseStatus();
            setStatus(s);
            // **仅展示，不作为本页持有**：本页只认自己 install 的租约。若本页租约已不在
            // Edge 当前槽位（被撤销 / 被覆盖 / 槽位清空）→ 本页失去持有（清掉 X-Lease-Id）。
            const cur = s.lease_id ?? null;
            const mine = api.leaseId;
            if (mine !== null && mine !== cur) {
                api.leaseId = null;
            }
            setMyLeaseId(mine !== null && mine === cur ? mine : null);
            if (cur !== null && mine === cur && s.state === "expired") {
                setExpiredNotice("本页租约已过期：受控操作已暂停，请续约或重新激活。");
            } else {
                setExpiredNotice(null);
            }
            setError(null);
        } catch (e) {
            setError(errMsg(e));
        }
    }, [api]);

    // 连接 / 断开时刷新；连接期间每 5s 轮询（捕捉过期 / 被其它会话接管等远端变化）
    useEffect(() => {
        if (!api) {
            setStatus(null);
            setCountdown(null);
            setMyLeaseId(null);
            return;
        }
        void refresh();
        const id = window.setInterval(() => void refresh(), 5000);
        return () => window.clearInterval(id);
    }, [api, refresh]);

    // 到期倒计时（每秒）—— Edge 当前租约的 expires_at（仅展示）
    useEffect(() => {
        const expires = status?.expires_at ? new Date(status.expires_at).getTime() : null;
        if (expires === null) {
            setCountdown(null);
            return;
        }
        const tick = () => setCountdown(Math.max(0, Math.floor((expires - Date.now()) / 1000)));
        tick();
        const id = window.setInterval(tick, 1000);
        return () => window.clearInterval(id);
    }, [status?.expires_at]);

    /** 本页签发租约：表单（字段 + 持续时间）→ 计算 expires_at → POST /v1/leases → 本页持有。 */
    const install = useCallback(
        async (values: LeaseFormValues) => {
            if (!api) return;
            setBusy(true);
            try {
                const payload: LeaseInstallRequest = {
                    lease_id: values.lease_id,
                    edge_id: values.edge_id,
                    holder_subject_id: values.holder_subject_id,
                    purpose: values.purpose,
                    state: values.state,
                    lease_version: 1,
                    expires_at: new Date(Date.now() + values.ttl * 1000).toISOString(),
                    ttl: values.ttl,
                };
                // installLease 已把 api.leaseId 设为新租约（本页签发）；refresh 仅校准展示
                const res = await api.installLease(payload);
                setMyLeaseId(res.lease_id);
                await refresh();
                setError(null);
            } catch (e) {
                setError(errMsg(e));
            } finally {
                setBusy(false);
            }
        },
        [api, refresh]
    );

    /** 续约 / 重新激活**本页租约**（心跳刷新）：lease_version+1，expires_at 重置为「当下 + ttl」。 */
    const renew = useCallback(async () => {
        if (!api || !myLeaseId) return;
        try {
            const ttl = status?.ttl ?? status?.default_ttl ?? 120;
            // 心跳刷新：每次续约把剩余有效期重置为 ttl（不再在旧到期上累加，
            // 避免 60s 续一次、每次 +120s 导致剩余时间无限累积 / 持有时间失控）。
            // 停续后租约最多 ttl 秒内过期，持有时间可控。已过期租约经此从当下重新激活。
            const nextExpiry = Date.now() + ttl * 1000;
            await api.renewLease((status?.lease_version ?? 0) + 1, new Date(nextExpiry).toISOString());
            await refresh();
            setError(null);
        } catch (e) {
            // 403 / 404 / 410：版本回退 / 不存在 / 过期冲突——保持现状，下轮 refresh 校准
            setError(errMsg(e));
        }
    }, [api, refresh, myLeaseId, status?.lease_version, status?.ttl, status?.default_ttl]);

    // 自动续租（本页持有租约的心跳）：按 renew_interval 定时；本页失去持有 / 关闭自动续租时停止
    useEffect(() => {
        if (renewTimer.current !== null) {
            window.clearInterval(renewTimer.current);
            renewTimer.current = null;
        }
        if (!autoRenew || !api || !holding) return;
        const interval = Math.max(1, status?.renew_interval || 60) * 1000;
        renewTimer.current = window.setInterval(() => void renew(), interval);
        return () => {
            if (renewTimer.current !== null) {
                window.clearInterval(renewTimer.current);
                renewTimer.current = null;
            }
        };
    }, [autoRenew, api, holding, status?.renew_interval, renew]);

    /** 撤销**本页租约**：POST /v1/leases/{id}:revoke → Edge 槽位清空，本页不再持有。 */
    const revoke = useCallback(async () => {
        if (!api || !myLeaseId) return;
        setBusy(true);
        try {
            await api.revokeLease(); // 内部已把 api.leaseId 置 null
            setMyLeaseId(null);
            await refresh();
            setError(null);
        } catch (e) {
            setError(errMsg(e));
        } finally {
            setBusy(false);
        }
    }, [api, refresh, myLeaseId]);

    /** 撤销占用租约并接管：撤销**其它会话**的当前占用租约（显式动作，绝不自动接管）。 */
    const takeover = useCallback(async () => {
        if (!api) return;
        const id = status?.lease_id;
        if (!id) return;
        setBusy(true);
        try {
            await api.revokeLeaseById(id);
            await refresh();
            setError(null);
        } catch (e) {
            setError(errMsg(e));
        } finally {
            setBusy(false);
        }
    }, [api, refresh, status?.lease_id]);

    return {
        status,
        countdown,
        autoRenew,
        setAutoRenew,
        busy,
        error,
        expiredNotice,
        myLeaseId,
        holding,
        expiredMine,
        occupiedByOther,
        canIssue,
        install,
        renew,
        revoke,
        takeover,
        refresh,
    };
}
