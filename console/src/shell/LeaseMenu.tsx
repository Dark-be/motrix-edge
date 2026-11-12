import { useMemo, useState, type FormEvent } from "react";

import { useEdge } from "../edge";
import type { LeaseFormValues } from "../hooks/useLease";
import { Button, ErrorText, Switch, fmtDuration, fmtTime, truncateId } from "../components/ui";
import { Dropdown } from "./Dropdown";
import { Chip, FormField, inputCls, type ChipTone } from "./controls";

const PURPOSES = ["capture", "rollout", "maintenance"];

function newLeaseId(): string {
    return `ls_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

/**
 * 顶栏「租约」下拉：**收起态只显示租约状态**（无租约 → 提示下拉签发）。
 *
 * 展开后：本页持有 → 倒计时 / 续租 / 撤销 / 自动续租；他人占用 → 显式「撤销占用并接管」；
 * 无租约 → 签发表单（Edge id 自动绑定当前连接；用户只选操作员 / 用途 / 有效期）。
 */
export function LeaseMenu() {
    const { lease, health, authStatus, auth } = useEdge();
    const {
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
    } = lease;

    const [holder, setHolder] = useState("console");
    const [purpose, setPurpose] = useState("capture");
    const [ttl, setTtl] = useState<number | null>(null);
    const [armTakeover, setArmTakeover] = useState(false);

    const isMine = myLeaseId !== null && myLeaseId === status?.lease_id;
    const defaultEdgeId = health?.identity?.["X-Edge-Id"] ?? "";
    const authenticatedHolder = auth?.user.subject ?? "";
    const effectiveHolder = authStatus?.enabled ? authenticatedHolder : holder;
    const effectiveTtl = ttl ?? status?.default_ttl ?? 120;
    const expectedExpiry = useMemo(() => new Date(Date.now() + effectiveTtl * 1000), [effectiveTtl]);

    const tone: ChipTone = holding
        ? countdown !== null && countdown <= 15
            ? "red"
            : countdown !== null && countdown <= 60
            ? "amber"
            : "green"
        : expiredMine
        ? "amber"
        : occupiedByOther
        ? "amber"
        : "slate";

    const label = holding ? "本页持有" : expiredMine ? "本页已过期" : occupiedByOther ? "其他页占用" : "无租约";
    const value = holding && countdown !== null ? fmtDuration(countdown) : undefined;

    const onSubmit = (e: FormEvent) => {
        e.preventDefault();
        if (!defaultEdgeId) return;
        const values: LeaseFormValues = {
            lease_id: newLeaseId(),
            edge_id: defaultEdgeId,
            holder_subject_id: effectiveHolder,
            purpose,
            state: "active",
            ttl: Number(effectiveTtl),
        };
        void install(values);
    };

    return (
        <Dropdown
            title="租约：区分本页持有、其他页面占用和 Edge 空闲"
            panelClass="w-[21rem]"
            label={<Chip tone={tone} label={label} value={value} />}
        >
            <div className="space-y-2">
                {status?.lease_id ? (
                    <div className="space-y-1 rounded border border-line bg-panel2 p-2 text-[11px]">
                        <Row k="占用者" v={status.holder_subject_id || "—"} />
                        <Row k="用途" v={status.purpose || "—"} />
                        <Row k="租约号" v={truncateId(status.lease_id, 18)} />
                        <Row k="版本" v={String(status.lease_version ?? "—")} />
                        <Row
                            k="到期"
                            v={`${fmtTime(status.expires_at)}${
                                countdown !== null ? ` · ${fmtDuration(countdown)}` : ""
                            }`}
                        />
                        <Row k="续租间隔" v={`${status.renew_interval}s`} />
                    </div>
                ) : (
                    <p className="rounded border border-line bg-panel2 p-2 text-[11px] text-muted">
                        Edge 槽位为空 → 在下面签发租约即可开始操作（观测预览 / 进入会话 / 下发动作都受租约约束）。
                    </p>
                )}

                {expiredNotice && <p className="text-[11px] text-amber-400">{expiredNotice}</p>}

                {isMine || expiredMine ? (
                    <div className="flex flex-wrap items-center gap-1.5">
                        <Button variant="primary" size="xs" disabled={!isMine || busy} onClick={() => void renew()}>
                            续租
                        </Button>
                        <Button variant="danger" size="xs" disabled={busy} onClick={() => void revoke()}>
                            撤销租约
                        </Button>
                        <div className="ml-auto">
                            <Switch checked={autoRenew} onChange={setAutoRenew} label="自动续租" />
                        </div>
                    </div>
                ) : occupiedByOther ? (
                    <div className="space-y-1.5">
                        <p className="text-[11px] text-amber-400">
                            租约被其它会话占用（{status?.holder_subject_id || "未知"}
                            ）。接管会先撤销对方租约——请确认对方已停止操作。
                        </p>
                        <div className="flex items-center gap-1.5">
                            <Switch checked={armTakeover} onChange={setArmTakeover} label="确认接管" />
                            <Button
                                variant="danger"
                                size="xs"
                                disabled={!armTakeover || busy}
                                onClick={() => void takeover()}
                            >
                                撤销占用并接管
                            </Button>
                        </div>
                    </div>
                ) : null}

                {canIssue && !isMine && !expiredMine && (
                    <form className="space-y-1.5 border-t border-line pt-2" onSubmit={onSubmit}>
                        <div className="rounded border border-line bg-panel2 px-2 py-1 text-[11px]">
                            <Row k="Edge" v={defaultEdgeId || "等待 Edge health"} />
                        </div>
                        <div className="grid grid-cols-2 gap-1.5">
                            <FormField label="操作员">
                                <input
                                    className={inputCls}
                                    value={authStatus?.enabled ? auth?.user.display_name ?? "请先飞书登录" : holder}
                                    disabled={authStatus?.enabled}
                                    onChange={(e) => setHolder(e.target.value)}
                                />
                            </FormField>
                            <FormField label="用途">
                                <select
                                    className={inputCls}
                                    value={purpose}
                                    onChange={(e) => setPurpose(e.target.value)}
                                >
                                    {PURPOSES.map((p) => (
                                        <option key={p} value={p}>
                                            {p}
                                        </option>
                                    ))}
                                </select>
                            </FormField>
                        </div>
                        <div className="grid grid-cols-2 gap-1.5">
                            <FormField label="有效期 (s)">
                                <input
                                    className={inputCls}
                                    type="number"
                                    min={5}
                                    value={effectiveTtl}
                                    onChange={(e) => setTtl(Number(e.target.value))}
                                />
                            </FormField>
                            <FormField label="预计到期">
                                <span className="tnum py-1 font-mono text-[11px] text-muted">
                                    {fmtTime(expectedExpiry.toISOString())}
                                </span>
                            </FormField>
                        </div>
                        <Button
                            variant="primary"
                            type="submit"
                            disabled={busy || !defaultEdgeId || (authStatus?.enabled === true && !auth)}
                            className="w-full"
                        >
                            签发租约
                        </Button>
                    </form>
                )}

                {error && <ErrorText message={error} />}
                <p className="text-[10px] text-faint">
                    受控操作（进入会话 / 采集控制 / 推理 / 手动下发 / 预览）都需持租约；自动续租按{" "}
                    <code className="font-mono">renew_interval</code> 心跳。租约细节记入左下角日志。
                </p>
            </div>
        </Dropdown>
    );
}

function Row({ k, v }: { k: string; v: string }) {
    return (
        <div className="flex items-baseline justify-between gap-2">
            <span className="text-[10px] uppercase tracking-wide text-faint">{k}</span>
            <span className="tnum min-w-0 truncate font-mono text-[11px] text-ink" title={v}>
                {v}
            </span>
        </div>
    );
}
