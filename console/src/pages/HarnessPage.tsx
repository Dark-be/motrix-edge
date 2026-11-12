import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge, Button, Card } from "../components/ui";
import { CameraRail } from "../layout/CameraRail";
import { PageLayout } from "../layout/PageLayout";
import { StatusStrip } from "../shell/StatusStrip";

type Provider = { id: string; label: string; kind: string };
type TaskField = { name: string; choices?: string[]; suggestions?: string[] };
type HarnessSpec = {
    task: { command: string; usage: string; fields: TaskField[]; display: string };
    runtime_components: Array<{ name: string; label: string; scope: string }>;
};
type HarnessConfig = { planner: string | null; model: string | null };
type TranscriptEvent = {
    type?: string;
    text?: string;
    tool?: string;
    args?: unknown;
    result?: unknown;
    [key: string]: unknown;
};
type HarnessState = {
    state: string;
    session_state: string;
    current_task: { label: string } | null;
    pending_task: { label: string } | null;
    runtime: Record<string, { status: string; error: string | null }>;
    usage: { in: number; out: number; tool_calls: number };
    frame_idx: number;
    frame_available: Record<string, boolean>;
    n_events: number;
    n_steps: number;
    error: string | null;
    control_error: string | null;
    interaction: {
        input_mode: "command_only" | "conversation" | "disabled";
        planner_activity: string;
        interrupt_requested: boolean;
        messages: Array<{ message_id: string; text: string; status: string; error?: string | null }>;
        last_error: string | null;
    };
    timeline: unknown[];
};

async function request<T>(url: string, init?: RequestInit): Promise<T> {
    const response = await fetch(url, {
        ...init,
        headers: init?.body ? { "Content-Type": "application/json", ...init.headers } : init?.headers,
    });
    const text = await response.text();
    let body: unknown = null;
    if (text) {
        try {
            body = JSON.parse(text);
        } catch {
            body = text;
        }
    }
    if (!response.ok) {
        const record = body && typeof body === "object" ? (body as Record<string, unknown>) : null;
        throw new Error(String(record?.detail ?? record?.error ?? body ?? `${response.status} ${response.statusText}`));
    }
    return body as T;
}

function json(value: unknown): string {
    try {
        return JSON.stringify(value, null, 2);
    } catch {
        return String(value);
    }
}

function TranscriptText({ text, label, code = false }: { text: string; label: string; code?: boolean }) {
    const long = text.length > 600 || text.split("\n").length > 12;
    const content = code ? (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-panel2 p-2 text-[10px] text-muted">
            {text}
        </pre>
    ) : (
        <p className="whitespace-pre-wrap text-ink">{text}</p>
    );
    if (!long) return content;
    return (
        <details>
            <summary className="cursor-pointer text-[10px] text-muted">
                {label} · {text.length.toLocaleString()} 字符
            </summary>
            <div className="mt-1">{content}</div>
        </details>
    );
}

function EventCard({ event, frameUrls }: { event: TranscriptEvent; frameUrls: Array<{ name: string; url: string }> }) {
    const type = event.type ?? "event";
    const imageTool =
        type === "tool_result" &&
        (/(view|camera|observation|state|render|image)/i.test(event.tool ?? "") ||
            json(event.result).includes("image"));
    const tone =
        type === "thinking" ? "amber" : type === "tool_call" ? "blue" : type === "tool_result" ? "green" : "slate";
    return (
        <article className="space-y-1.5 rounded border border-line bg-panel p-2 text-[11px]">
            <div className="flex items-center gap-2">
                <Badge tone={tone}>{type}</Badge>
                {event.tool && <span className="font-mono text-[10px] text-accent">{event.tool}</span>}
            </div>
            {event.text && <TranscriptText text={event.text} label={type === "thinking" ? "思考过程" : "消息"} />}
            {event.args !== undefined && (
                <details>
                    <summary className="cursor-pointer text-[10px] text-muted">调用参数</summary>
                    <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-panel2 p-2 text-[10px] text-muted">
                        {json(event.args)}
                    </pre>
                </details>
            )}
            {event.result !== undefined && (
                <TranscriptText
                    text={typeof event.result === "string" ? event.result : json(event.result)}
                    label="工具结果"
                    code
                />
            )}
            {imageTool && frameUrls.length > 0 && (
                <div className="flex gap-1.5 overflow-x-auto pt-1">
                    {frameUrls.map((frame) => (
                        <figure key={frame.name} className="w-28 shrink-0">
                            <img
                                src={frame.url}
                                alt={frame.name}
                                className="aspect-video w-full rounded bg-black object-contain"
                            />
                            <figcaption className="truncate pt-0.5 text-[9px] text-faint" title={frame.name}>
                                当前帧 · {frame.name}
                            </figcaption>
                        </figure>
                    ))}
                </div>
            )}
            {!event.text && event.args === undefined && event.result === undefined && (
                <TranscriptText text={json(event)} label="原始事件" code />
            )}
        </article>
    );
}

/** 05 HARNESS —— Console-owned Agent UI backed by an allowlisted provider. */
export function HarnessPage() {
    const [providers, setProviders] = useState<Provider[]>([]);
    const [providerId, setProviderId] = useState("");
    const [spec, setSpec] = useState<HarnessSpec | null>(null);
    const [config, setConfig] = useState<HarnessConfig | null>(null);
    const [state, setState] = useState<HarnessState | null>(null);
    const [events, setEvents] = useState<unknown[]>([]);
    const [taskId, setTaskId] = useState("");
    const [message, setMessage] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const base = providerId ? `/api/harness/providers/${encodeURIComponent(providerId)}` : "";
    const taskField = spec?.task.fields.find((field) => field.name === "task_id") ?? spec?.task.fields[0];
    const tasks = useMemo(
        () =>
            (taskField?.choices ?? []).map((value, index) => ({
                value,
                label: taskField?.suggestions?.[index] ?? value,
            })),
        [taskField]
    );

    const loadProvider = useCallback(async () => {
        if (!base) return;
        try {
            const [nextSpec, nextConfig, nextState, transcript] = await Promise.all([
                request<HarnessSpec>(`${base}/api/commands`),
                request<HarnessConfig>(`${base}/api/session/config`),
                request<HarnessState>(`${base}/api/session/state`),
                request<{ events: unknown[] }>(`${base}/api/session/transcript?since=0`),
            ]);
            setSpec(nextSpec);
            setConfig(nextConfig);
            setState(nextState);
            setEvents(transcript.events);
            setTaskId((current) => current || nextSpec.task.fields[0]?.choices?.[0] || "");
            setError(null);
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : String(cause));
        }
    }, [base]);

    useEffect(() => {
        void request<{ providers: Provider[] }>("/api/harness/providers")
            .then(({ providers: next }) => {
                setProviders(next);
                setProviderId((current) => current || next[0]?.id || "");
                setError(next.length ? null : "Console 尚未配置 Harness Provider");
            })
            .catch((cause) => setError(cause instanceof Error ? cause.message : String(cause)));
    }, []);

    useEffect(() => {
        setSpec(null);
        setConfig(null);
        setState(null);
        setEvents([]);
        if (!base) return;
        void loadProvider();
        const timer = window.setInterval(() => void loadProvider(), 1000);
        return () => window.clearInterval(timer);
    }, [base, loadProvider]);

    const post = async (path: string, body?: unknown) => {
        if (!base) return;
        await request(`${base}${path}`, {
            method: "POST",
            ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        await loadProvider();
    };

    const startTask = async () => {
        if (!spec || !taskId) return;
        setBusy(true);
        try {
            await post("/api/session/messages", { text: `${spec.task.command} ${taskId}` });
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : String(cause));
        } finally {
            setBusy(false);
        }
    };

    const sendMessage = async () => {
        const text = message.trim();
        if (!text) return;
        setBusy(true);
        try {
            await post("/api/session/messages", { text });
            setMessage("");
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : String(cause));
        } finally {
            setBusy(false);
        }
    };

    const interrupt = async () => {
        setBusy(true);
        try {
            await post("/api/session/interrupt");
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : String(cause));
        } finally {
            setBusy(false);
        }
    };

    const stopTask = async () => {
        setBusy(true);
        try {
            await post("/api/session/stop");
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : String(cause));
        } finally {
            setBusy(false);
        }
    };

    const frameUrls = Object.keys(state?.frame_available ?? {}).map((name) => ({
        name,
        url: `${base}/api/session/frame?kind=${encodeURIComponent(name)}&v=${state?.frame_idx ?? -1}`,
    }));
    const conversation = state?.interaction.input_mode === "conversation";
    const taskActive = state?.state === "starting" || state?.state === "running";
    const plannerBusy = state?.interaction.planner_activity === "busy";
    const displayedError = error ?? state?.control_error ?? state?.error ?? state?.interaction.last_error;

    return (
        <>
            <StatusStrip
                mode="HARNESS"
                tone="green"
                live={{
                    ok: Boolean(state) && !displayedError,
                    label: state ? `${providerId} · ${state.state}` : "Provider 未连接",
                }}
                stage={`STEP ${state?.n_steps ?? 0} · ${state?.n_events ?? 0} EVENTS`}
            />
            <PageLayout
                left={
                    <Card title="Harness" subtitle="Agent Provider · TaskRun">
                        <div className="space-y-3">
                            <div>
                                <label className="mb-1 block text-[10px] uppercase tracking-wide text-faint">
                                    Provider
                                </label>
                                <select
                                    className="w-full rounded border border-line2 bg-panel2 px-2 py-1 text-[11px] text-ink"
                                    value={providerId}
                                    onChange={(event) => setProviderId(event.target.value)}
                                >
                                    {providers.map((provider) => (
                                        <option key={provider.id} value={provider.id}>
                                            {provider.label} · {provider.kind}
                                        </option>
                                    ))}
                                </select>
                            </div>
                            <div className="border-t border-line pt-2">
                                <label className="mb-1 block text-[10px] uppercase tracking-wide text-faint">
                                    Task
                                </label>
                                <select
                                    className="w-full rounded border border-line2 bg-panel2 px-2 py-1 text-[11px] text-ink"
                                    value={taskId}
                                    onChange={(event) => setTaskId(event.target.value)}
                                    disabled={!tasks.length}
                                >
                                    {tasks.map((task) => (
                                        <option key={task.value} value={task.value}>
                                            {task.label}
                                        </option>
                                    ))}
                                </select>
                                <p className="mt-1 truncate font-mono text-[9px] text-faint" title={spec?.task.usage}>
                                    {spec?.task.usage ?? "等待 Provider 任务目录"}
                                </p>
                            </div>
                            <div className="rounded bg-panel2 p-2 text-[10px] text-muted">
                                <div className="flex items-center justify-between gap-2">
                                    <span>planner</span>
                                    <span className="truncate font-mono text-ink">{config?.planner ?? "—"}</span>
                                </div>
                                <div className="flex items-center justify-between gap-2">
                                    <span>model</span>
                                    <span className="truncate font-mono text-ink" title={config?.model ?? undefined}>
                                        {config?.model ?? "Provider default"}
                                    </span>
                                </div>
                            </div>
                            <div className="flex flex-wrap gap-2">
                                <Button
                                    variant="primary"
                                    onClick={() => void startTask()}
                                    disabled={!taskId || busy || !spec}
                                >
                                    启动任务
                                </Button>
                                <Button variant="danger" onClick={() => void stopTask()} disabled={!taskActive || busy}>
                                    终止任务
                                </Button>
                            </div>
                            <div className="space-y-1 border-t border-line pt-2 text-[10px] text-muted">
                                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                                    <span>
                                        session{" "}
                                        <span className="font-mono text-ink">{state?.session_state ?? "—"}</span>
                                    </span>
                                    <span>
                                        agent{" "}
                                        <span className="font-mono text-ink">
                                            {state?.interaction.planner_activity ?? "—"}
                                        </span>
                                    </span>
                                    <span>
                                        tools <span className="font-mono text-ink">{state?.usage.tool_calls ?? 0}</span>
                                    </span>
                                </div>
                                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                                    {Object.entries(state?.runtime ?? {}).map(([name, value]) => (
                                        <span key={name} className="inline-flex items-center gap-1">
                                            {name}
                                            <Badge
                                                tone={
                                                    value.status === "ready"
                                                        ? "green"
                                                        : value.status === "failed"
                                                        ? "red"
                                                        : "slate"
                                                }
                                            >
                                                {value.status}
                                            </Badge>
                                        </span>
                                    ))}
                                </div>
                                <div className="font-mono text-faint">
                                    token in {state?.usage.in ?? 0} · out {state?.usage.out ?? 0} ·{" "}
                                    {state?.n_events ?? 0} events
                                </div>
                            </div>
                            {state?.current_task && (
                                <p className="text-[11px] text-muted">当前：{state.current_task.label}</p>
                            )}
                            {state?.pending_task && (
                                <p className="text-[11px] text-amber-600">等待：{state.pending_task.label}</p>
                            )}
                            {displayedError && (
                                <p className="whitespace-pre-wrap rounded border border-rose-500/30 bg-rose-500/10 p-2 text-[11px] text-rose-600">
                                    {displayedError}
                                </p>
                            )}
                        </div>
                    </Card>
                }
                center={
                    <Card
                        title="Agent 对话与事件"
                        subtitle={state?.interaction.planner_activity ?? "等待任务"}
                        className="flex min-h-0 flex-1 flex-col"
                        contentClassName="min-h-0 flex-1 overflow-hidden"
                    >
                        <div className="flex h-full min-h-0 flex-col gap-2">
                            <div className="min-h-0 flex-1 touch-pan-y space-y-2 overflow-y-auto overscroll-contain rounded border border-line bg-panel2 p-2">
                                {events.length === 0 && <p className="text-[11px] text-muted">暂无 Agent 事件。</p>}
                                {events.map((event, index) => (
                                    <EventCard key={index} event={event as TranscriptEvent} frameUrls={frameUrls} />
                                ))}
                                {(state?.interaction.messages ?? []).map((item) => (
                                    <div key={item.message_id} className="rounded bg-sky-500/10 p-2 text-[11px]">
                                        <span className="mr-2 text-[9px] uppercase text-faint">{item.status}</span>
                                        {item.text}
                                    </div>
                                ))}
                            </div>
                            <div className="flex gap-2">
                                <textarea
                                    className="min-h-16 flex-1 resize-y rounded border border-line2 bg-panel2 p-2 text-[11px] text-ink"
                                    value={message}
                                    placeholder={
                                        conversation ? "向当前 Agent 发送后续指令" : "任务运行并接受输入后可对话"
                                    }
                                    onChange={(event) => setMessage(event.target.value)}
                                    disabled={!conversation}
                                />
                                <div className="flex flex-col gap-2">
                                    <Button
                                        variant="primary"
                                        onClick={() => void sendMessage()}
                                        disabled={!conversation || !message.trim() || busy}
                                    >
                                        发送
                                    </Button>
                                    <Button onClick={() => void interrupt()} disabled={!plannerBusy || busy}>
                                        中断当前调用
                                    </Button>
                                </div>
                            </div>
                        </div>
                    </Card>
                }
                right={<CameraRail />}
            />
        </>
    );
}
