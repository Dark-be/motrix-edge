import { useEffect, useState } from "react";

import { EdgeApi } from "./api";
import { EdgeProvider, useEdge } from "./edge";
import { isLocalEdgeUrl, LOCAL_EDGE_URL } from "./edgeTargets";
import { ThemeProvider } from "./theme";
import { LogPanel } from "./components/LogPanel";
import { Toasts } from "./components/Toasts";
import { Button, Card } from "./components/ui";
import { TabBar, type PageId } from "./shell/TabBar";
import { TopBar } from "./shell/TopBar";
import { CollectPage } from "./pages/CollectPage";
import { DebugPage } from "./pages/DebugPage";
import { InferPage } from "./pages/InferPage";
import { ManualPage } from "./pages/ManualPage";
import { errMsg } from "./types";

/**
 * 控制台外壳：**顶栏（连接 / 租约 / 节点 / 急停）→ 标签栏 → 页面 → 日志抽屉 + toast**。
 *
 * 页面按标签独立成页：`01 MANUAL · 02 COLLECT · 03 INFER · 04 DEBUG`。
 * LLM / Replay / Result 暂时不进入导航，页面源码保留供后续恢复。
 */
export default function App() {
    return (
        <ThemeProvider>
            <EdgeProvider>
                <Shell />
            </EdgeProvider>
        </ThemeProvider>
    );
}

function Shell() {
    const { connected, logs, clearLogs, toasts, dismissToast } = useEdge();
    const [page, setPage] = useState<PageId>("manual");

    return (
        <div className="flex h-screen flex-col overflow-hidden bg-page text-ink">
            <TopBar />
            <TabBar active={page} onChange={setPage} />
            {connected ? <PageBody page={page} /> : <ConnectGate />}
            <LogPanel entries={logs} onClear={clearLogs} />
            <Toasts toasts={toasts} onDismiss={dismissToast} />
        </div>
    );
}

function PageBody({ page }: { page: PageId }) {
    switch (page) {
        case "manual":
            return <ManualPage />;
        case "collect":
            return <CollectPage />;
        case "infer":
            return <InferPage />;
        case "debug":
            return <DebugPage />;
    }
}

/** 未连接时的落地页：填 Edge 地址并连接（地址记在 localStorage，刷新自动重连）。 */
function ConnectGate() {
    const { baseUrl, connect, healthError } = useEdge();
    const [draft, setDraft] = useState(baseUrl || LOCAL_EDGE_URL);
    const [edges, setEdges] = useState<Array<{ id: string; label: string }>>([]);
    const [authenticated, setAuthenticated] = useState<boolean | null>(null);
    const [consoleError, setConsoleError] = useState<string | null>(null);

    useEffect(() => {
        const consoleApi = new EdgeApi(`${window.location.origin}/api/edges/_bootstrap`);
        void consoleApi
            .authStatus()
            .then(() => consoleApi.authMe())
            .then(() => consoleApi.consoleEdges())
            .then((response) => {
                setAuthenticated(true);
                setEdges(response.edges);
                if (!baseUrl && response.edges.length > 0) {
                    setDraft(`${window.location.origin}/api/edges/${encodeURIComponent(response.edges[0].id)}`);
                }
                setConsoleError(null);
            })
            .catch((error) => {
                setAuthenticated(false);
                setConsoleError(errMsg(error));
            });
    }, [baseUrl]);

    const loginUrl = `${window.location.origin}/api/auth/feishu/login?${new URLSearchParams({
        return_to: window.location.pathname,
    })}`;

    const canConnect = isLocalEdgeUrl(draft) || (authenticated === true && edges.length > 0);

    return (
        <div className="flex flex-1 items-start justify-center overflow-y-auto p-6">
            <Card title="连接 Edge" subtitle="Motrix Edge Console" className="w-full max-w-2xl">
                <div className="space-y-3 text-[11px] text-muted">
                    {authenticated === false && (
                        <div className="flex items-center justify-between gap-2 rounded border border-amber-700/60 bg-amber-950/30 p-2">
                            <span>请先登录飞书，再选择可访问的 Edge。{consoleError ? `（${consoleError}）` : ""}</span>
                            <Button variant="primary" onClick={() => window.location.assign(loginUrl)}>
                                使用飞书登录
                            </Button>
                        </div>
                    )}
                    {(authenticated || isLocalEdgeUrl(draft)) && (
                        <label className="flex flex-col gap-1">
                            <span className="text-[10px] uppercase tracking-wide text-faint">Edge 目标</span>
                            <select
                                className="rounded border border-line2 bg-panel2 px-2 py-1 text-[11px] text-ink"
                                value={draft}
                                onChange={(event) => setDraft(event.target.value)}
                            >
                                <option value={LOCAL_EDGE_URL}>本机 Edge · localhost:8000（直连）</option>
                                {authenticated &&
                                    edges.map((edge) => {
                                        const value = `${window.location.origin}/api/edges/${encodeURIComponent(
                                            edge.id
                                        )}`;
                                        return (
                                            <option key={edge.id} value={value}>
                                                {edge.label} · {edge.id}
                                            </option>
                                        );
                                    })}
                            </select>
                        </label>
                    )}
                    {authenticated && edges.length === 0 && (
                        <p className="rounded border border-line bg-panel2 p-2 text-amber-400">
                            Console 尚未配置远端 Edge；仍可直连本机 localhost:8000。
                        </p>
                    )}
                    <div className="flex flex-wrap items-center gap-2">
                        <input
                            className="min-w-0 flex-1 rounded border border-line2 bg-panel2 px-2 py-1 font-mono text-[11px] text-ink focus:border-accent focus:outline-none"
                            value={draft}
                            placeholder={LOCAL_EDGE_URL}
                            onChange={(e) => setDraft(e.target.value)}
                            onKeyDown={(e) => {
                                if (e.key === "Enter") connect(draft);
                            }}
                        />
                        <Button variant="primary" disabled={!canConnect} onClick={() => connect(draft)}>
                            连接
                        </Button>
                    </div>
                    {healthError && <p className="text-amber-400">{healthError}</p>}
                    <p>
                        受控操作（进入会话 / 采集轮次 / 推理 / 手动下发 / 观测预览 / 视频流）都需先签发
                        <b>租约</b>——顶栏「租约」下拉里签发，页面会按{" "}
                        <code className="font-mono text-accent">renew_interval</code> 自动续租。
                    </p>
                    <p>
                        页面分工：
                        <b>01 MANUAL</b> 滑块手动下发 · <b>02 COLLECT</b> 数采 · <b>03 INFER</b> 推理 · <b>04 DEBUG</b>{" "}
                        诊断与上传打包。快捷键：
                        <code className="font-mono text-accent">Esc</code> = 急停（须持租约）。
                    </p>
                </div>
            </Card>
        </div>
    );
}
