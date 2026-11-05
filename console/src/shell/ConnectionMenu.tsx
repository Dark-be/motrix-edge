import { useState } from "react";

import { useEdge } from "../edge";
import { LOCAL_EDGE_URL } from "../edgeTargets";
import { Button } from "../components/ui";
import { Dropdown } from "./Dropdown";
import { Chip, FormField, inputCls } from "./controls";

/** 顶栏「连接」下拉：Edge 地址 + 连接 / 断开 + 节点身份与版本。 */
export function ConnectionMenu() {
    const { baseUrl, connect, disconnect, connected, health, healthError, api } = useEdge();
    const [draft, setDraft] = useState(baseUrl || LOCAL_EDGE_URL);

    const host = (() => {
        try {
            return new URL(baseUrl).host;
        } catch {
            return baseUrl || "未连接";
        }
    })();

    return (
        <Dropdown
            panelClass="w-[21rem]"
            title="Edge 连接"
            label={
                <Chip
                    tone={connected ? (healthError ? "amber" : "green") : "slate"}
                    label={connected ? "link" : "offline"}
                    value={host}
                />
            }
        >
            <div className="space-y-2">
                <FormField label="Edge 地址" hint="受控操作走 HTTP 控制面（默认 :8000）；页面会记住地址并自动重连。">
                    <input
                        className={inputCls}
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === "Enter") connect(draft);
                        }}
                        placeholder="http://localhost:8000"
                    />
                </FormField>
                <div className="flex items-center gap-1.5">
                    <Button variant="primary" size="xs" onClick={() => connect(draft)}>
                        连接
                    </Button>
                    <Button size="xs" disabled={!connected} onClick={disconnect}>
                        断开
                    </Button>
                </div>
                {connected && (
                    <div className="space-y-1 rounded border border-line bg-panel2 p-2 text-[11px]">
                        <Row k="状态" v={health?.status ?? (healthError ? "离线（数据已过期）" : "…")} />
                        <Row k="版本" v={health?.version ?? "—"} />
                        <Row k="Edge id" v={health?.identity?.["X-Edge-Id"] ?? "—"} />
                        <Row k="机器人" v={health?.robot?.name ?? "—"} />
                        <Row k="适配器" v={health?.robot?.type ?? "—"} />
                        <Row k="租约头" v={api?.leaseId ?? "（未持有）"} />
                        {health?.disk?.free !== undefined && (
                            <Row k="磁盘剩余" v={`${(health.disk.free / 1e9).toFixed(1)} GB`} />
                        )}
                    </div>
                )}
                {healthError && <p className="text-[11px] text-amber-400">{healthError}</p>}
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
