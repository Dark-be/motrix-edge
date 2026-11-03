import { useCallback, useEffect, useMemo, useState } from "react";

import { useEdge } from "../edge";
import { Button, Card, Disclosure, Field, FieldGrid, Badge } from "../components/ui";
import { StatusStrip } from "../shell/StatusStrip";
import type { RpentEnvelope, RpentInfoResponse } from "../types";

type ChatMessage = { id: number; role: "user" | "assistant" | "system"; text: string };

type Operation = {
    method: string;
    label: string;
    description: string;
    readOnly: boolean;
    defaultArgs: string;
};

const OPERATION_CATALOG: Record<string, Omit<Operation, "method">> = {
    "env.get_env_meta": {
        label: "读取环境能力",
        description: "查看动作空间、机械臂、相机和坐标系。",
        readOnly: true,
        defaultArgs: "{}",
    },
    "env.get_robot_state": {
        label: "读取机器人状态",
        description: "查看当前关节、夹爪和末端状态。",
        readOnly: true,
        defaultArgs: "{}",
    },
    "env.get_observation": {
        label: "读取当前观测",
        description: "读取当前观测快照（可能包含图像）。",
        readOnly: true,
        defaultArgs: "{}",
    },
    "env.get_camera_meta": {
        label: "读取相机信息",
        description: "查看相机内参、分辨率和坐标信息。",
        readOnly: true,
        defaultArgs: "{}",
    },
    "env.reset": {
        label: "复位机器人",
        description: "恢复机器人安全姿态，属于受控写操作。",
        readOnly: false,
        defaultArgs: "{}",
    },
    "env.move_delta": {
        label: "相对移动",
        description: "按 RPent 定义的增量移动机械臂。",
        readOnly: false,
        defaultArgs: '{\n  "delta": []\n}',
    },
    "env.move_to": {
        label: "移动到目标",
        description: "移动到给定的 TCP / 法兰目标位姿。",
        readOnly: false,
        defaultArgs: '{\n  "pose": []\n}',
    },
    "env.look_at": {
        label: "看向目标",
        description: "让指定机械臂末端朝向目标点。",
        readOnly: false,
        defaultArgs: '{\n  "target": []\n}',
    },
    "env.set_gripper": {
        label: "设置夹爪",
        description: "设置指定机械臂夹爪开合值。",
        readOnly: false,
        defaultArgs: '{\n  "value": 0\n}',
    },
};

function pretty(value: unknown): string {
    try {
        return JSON.stringify(value, null, 2) ?? String(value);
    } catch {
        return String(value);
    }
}

function operationFor(method: string): Operation {
    const known = OPERATION_CATALOG[method];
    return {
        method,
        ...(known ?? { label: method, description: "RPent 暴露的方法。", readOnly: false, defaultArgs: "{}" }),
    };
}

/** LLM / RPent 工作台：本地聊天编排 + RPent 动态能力调用。 */
export function LlmPage() {
    const { api, hasLease, onLog } = useEdge();
    const [info, setInfo] = useState<RpentInfoResponse | null>(null);
    const [meta, setMeta] = useState<unknown>(null);
    const [messages, setMessages] = useState<ChatMessage[]>([
        { id: 1, role: "assistant", text: "连接 RPent 后，我会根据它公开的能力生成可选操作。请先点击“连接 RPent”。" },
    ]);
    const [draft, setDraft] = useState("");
    const [busy, setBusy] = useState(false);
    const [reply, setReply] = useState<RpentEnvelope | null>(null);
    const [args, setArgs] = useState<Record<string, string>>({});

    const loadRpent = useCallback(async () => {
        if (!api) return;
        setBusy(true);
        try {
            const next = await api.rpentInfo();
            setInfo(next);
            const methods = new Set(next.methods);
            if (methods.has("env.get_env_meta")) {
                const envReply = await api.rpentCall("env.get_env_meta");
                if (envReply.ok) setMeta(envReply.result);
            }
            setMessages((old) => [
                ...old,
                {
                    id: Date.now(),
                    role: "assistant",
                    text: `RPent 已连接，发现 ${next.methods.length} 个方法。右侧操作由这份能力清单生成。`,
                },
            ]);
        } catch (error) {
            setMessages((old) => [
                ...old,
                {
                    id: Date.now(),
                    role: "system",
                    text: `连接 RPent 失败：${error instanceof Error ? error.message : String(error)}`,
                },
            ]);
        } finally {
            setBusy(false);
        }
    }, [api]);

    useEffect(() => {
        setInfo(null);
        setMeta(null);
    }, [api]);

    useEffect(() => {
        if (api) void loadRpent();
    }, [api, loadRpent]);

    const operations = useMemo(() => (info?.methods ?? []).map(operationFor), [info]);

    const call = useCallback(
        async (operation: Operation) => {
            if (!api) return;
            let payload: Record<string, unknown> = {};
            try {
                const raw = args[operation.method] ?? operation.defaultArgs;
                const parsed = JSON.parse(raw);
                if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed))
                    throw new Error("参数必须是 JSON 对象");
                payload = parsed as Record<string, unknown>;
            } catch (error) {
                setReply({
                    ok: false,
                    kind: "argument",
                    error: error instanceof Error ? error.message : String(error),
                });
                return;
            }
            setBusy(true);
            try {
                const result = await api.rpentCall(operation.method, payload);
                setReply(result);
                onLog(`${result.ok ? "✓" : "✗"} RPent ${operation.method}`, result.ok);
                setMessages((old) => [
                    ...old,
                    {
                        id: Date.now(),
                        role: "assistant",
                        text: result.ok
                            ? `${operation.label} 已完成。`
                            : `${operation.label} 未完成：${result.error ?? "RPent 返回失败"}`,
                    },
                ]);
            } catch (error) {
                const message = error instanceof Error ? error.message : String(error);
                setReply({ ok: false, kind: "network", error: message });
                setMessages((old) => [
                    ...old,
                    { id: Date.now(), role: "system", text: `${operation.label} 请求失败：${message}` },
                ]);
            } finally {
                setBusy(false);
            }
        },
        [api, args, onLog]
    );

    const sendChat = () => {
        const text = draft.trim();
        if (!text) return;
        setMessages((old) => [...old, { id: Date.now(), role: "user", text }]);
        const lower = text.toLowerCase();
        const match = operations.find(
            (op) =>
                (op.method + op.label + op.description).toLowerCase().includes(lower) ||
                (lower.includes("状态") && op.method === "env.get_robot_state") ||
                (lower.includes("观测") && op.method === "env.get_observation") ||
                (lower.includes("复位") && op.method === "env.reset")
        );
        setMessages((old) => [
            ...old,
            {
                id: Date.now() + 1,
                role: "assistant",
                text: match
                    ? `可以使用“${match.label}”。请在右侧检查参数后点击执行；我不会因聊天内容自动驱动机器人。`
                    : "我可以根据当前 RPent 能力帮你定位操作。请描述状态、观测、复位、移动或夹爪需求，或直接选择右侧操作。",
            },
        ]);
        setDraft("");
    };

    return (
        <>
            <StatusStrip
                mode="LLM / RPENT"
                tone="blue"
                live={{ ok: Boolean(info?.enabled), label: info ? "RPent ONLINE" : "未连接 RPent" }}
                stage={`${info?.methods.length ?? 0} METHODS`}
                actions={
                    <Button size="xs" variant="primary" onClick={() => void loadRpent()} disabled={busy || !api}>
                        {busy ? "连接中…" : info ? "刷新能力" : "连接 RPent"}
                    </Button>
                }
            />
            <div className="grid min-h-0 flex-1 gap-3 overflow-y-auto p-3 xl:grid-cols-[minmax(0,1.1fr)_minmax(22rem,0.9fr)]">
                <Card title="对话" subtitle="本地编排 · 不自动执行机器人动作" className="min-h-[30rem]">
                    <div className="flex min-h-[25rem] flex-col gap-2">
                        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto rounded border border-slate-800 bg-slate-950/50 p-2">
                            {messages.map((message) => (
                                <div
                                    key={message.id}
                                    className={`max-w-[90%] rounded px-2 py-1.5 text-[11px] ${
                                        message.role === "user"
                                            ? "ml-auto bg-sky-900/40 text-sky-100"
                                            : message.role === "system"
                                            ? "bg-rose-950/40 text-rose-200"
                                            : "bg-slate-800/70 text-slate-200"
                                    }`}
                                >
                                    <span className="mr-1 text-[9px] uppercase text-slate-500">{message.role}</span>
                                    {message.text}
                                </div>
                            ))}
                        </div>
                        <div className="flex gap-2">
                            <input
                                value={draft}
                                onChange={(event) => setDraft(event.target.value)}
                                onKeyDown={(event) => {
                                    if (event.key === "Enter") sendChat();
                                }}
                                placeholder="例如：读取当前机器人状态"
                                className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-[11px] text-slate-100 focus:border-sky-500 focus:outline-none"
                            />
                            <Button variant="primary" onClick={sendChat}>
                                发送
                            </Button>
                        </div>
                    </div>
                </Card>
                <div className="min-w-0 space-y-3">
                    <Card title="RPent 能力" subtitle={info ? info.endpoint : "未读取"}>
                        {!info ? (
                            <p className="text-[11px] text-slate-500">连接后将按 RPent 返回的方法动态生成操作。</p>
                        ) : (
                            <>
                                <FieldGrid>
                                    <Field
                                        k="enabled"
                                        v={
                                            <Badge tone={info.enabled ? "green" : "amber"}>
                                                {String(info.enabled)}
                                            </Badge>
                                        }
                                    />
                                    <Field k="methods" v={info.methods.length} />
                                    <Field k="lease" v={hasLease ? "active" : "required for writes"} />
                                </FieldGrid>
                                {meta !== null && (
                                    <Disclosure label="环境元数据" defaultOpen>
                                        <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words text-[10px] text-slate-300">
                                            {pretty(meta)}
                                        </pre>
                                    </Disclosure>
                                )}
                            </>
                        )}
                    </Card>
                    <Card title="可选操作" subtitle="方法由 RPent 清单驱动">
                        <div className="space-y-2">
                            {operations.length === 0 && <p className="text-[11px] text-slate-500">暂无可用方法。</p>}
                            {operations.map((operation) => (
                                <div
                                    key={operation.method}
                                    className="rounded border border-slate-800 bg-slate-950/40 p-2"
                                >
                                    <div className="flex items-start gap-2">
                                        <div className="min-w-0 flex-1">
                                            <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-200">
                                                <span>{operation.label}</span>
                                                <Badge tone={operation.readOnly ? "green" : hasLease ? "amber" : "red"}>
                                                    {operation.readOnly
                                                        ? "只读"
                                                        : hasLease
                                                        ? "需租约·可执行"
                                                        : "需租约"}
                                                </Badge>
                                            </div>
                                            <p className="mt-0.5 text-[10px] text-slate-500">
                                                {operation.description}{" "}
                                                <code className="font-mono">{operation.method}</code>
                                            </p>
                                        </div>
                                        <Button
                                            size="xs"
                                            variant={operation.readOnly ? "default" : "primary"}
                                            disabled={busy || (!operation.readOnly && !hasLease)}
                                            title={!operation.readOnly && !hasLease ? "请先在顶栏签发租约" : undefined}
                                            onClick={() => void call(operation)}
                                        >
                                            执行
                                        </Button>
                                    </div>
                                    <textarea
                                        value={args[operation.method] ?? operation.defaultArgs}
                                        onChange={(event) =>
                                            setArgs((old) => ({ ...old, [operation.method]: event.target.value }))
                                        }
                                        spellCheck={false}
                                        className="mt-1.5 min-h-12 w-full resize-y rounded border border-slate-800 bg-slate-950 px-2 py-1 font-mono text-[10px] text-slate-300 focus:border-sky-500 focus:outline-none"
                                    />
                                </div>
                            ))}
                        </div>
                    </Card>
                    {reply && (
                        <Card title="最近回执" subtitle={reply.ok ? "ok" : `${reply.kind ?? "error"}`}>
                            <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words text-[10px] text-slate-300">
                                {pretty(reply)}
                            </pre>
                        </Card>
                    )}
                </div>
            </div>
        </>
    );
}
