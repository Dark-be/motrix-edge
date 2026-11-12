import { useCallback, useEffect, useMemo, useState } from "react";

import { useEdge } from "../edge";
import { captureSyncMeta, missingCaptureMeta, REQUIRED_CAPTURE_META } from "../captureMeta";
import { Button, Card, ErrorText } from "../components/ui";
import { inputCls } from "../shell/controls";
import { errMsg, type CaptureMetaResponse } from "../types";

/** 飞书身份不属于 capture.yml；这里只排序现场可维护的业务分类。 */
const PREFERRED_ORDER = ["task_name"];
const LABELS: Record<string, string> = { task_name: "任务" };

/**
 * 下方只读展示的是**机器人进程当前持有的拍摄摘要**（即从 Edge 同步过去的那份 meta）：
 * 采集人 / 任务 + 录制结果（时长 / 帧数，episode 结束时写入）。
 *
 * 键名按候选顺序找第一个有值的；数值字段优先按数字格式化（**时长以机器人进程的 `duration`
 * 为准**，前端不计时）。数组顺序 = 展示顺序。
 */
const SYNCED_SUMMARY: { label: string; keys: string[]; format?: (v: number) => string }[] = [
    { label: "录制时长", keys: ["duration", "duration_s", "length_s"], format: (v) => `${v.toFixed(1)} s` },
    { label: "动作帧数", keys: ["frames", "n_frames", "num_frames", "frame_count"] },
    { label: "采集人", keys: ["operator"] },
    { label: "任务", keys: ["task_name", "task"] },
];

/** 已同步摘要取值（缺失 → `—`）。 */
function statValue(meta: Record<string, unknown>, item: (typeof SYNCED_SUMMARY)[number]): string {
    for (const key of item.keys) {
        const value = meta[key];
        if (value === null || value === undefined || value === "") continue;
        const num = typeof value === "number" ? value : Number(value);
        if (!Number.isFinite(num)) return String(value);
        return item.format ? item.format(num) : String(num);
    }
    return "—";
}

function orderedKeys(meta: Record<string, string[]>): string[] {
    const keys = Object.keys(meta);
    const head = PREFERRED_ORDER.filter((k) => keys.includes(k));
    return [...head, ...keys.filter((k) => !head.includes(k))];
}

const iconBtn =
    "flex h-5 w-5 shrink-0 items-center justify-center rounded border border-slate-700 text-[11px] text-slate-400 hover:border-slate-500 hover:text-slate-100 disabled:opacity-40";

/**
 * 采集元信息（`capture meta`）：**分类 → 下拉选择** + 就地小按钮（新增 / 重命名 / 删除选项）
 * + 「同步」推给机器人进程（同步只在采集会话内被接受）；下方只读展示**已同步到机器人进程的摘要**
 * （录制时长 / 动作帧数 / 采集人 / 任务，5 Hz 轮询 `GET /v1/captures` 刷新）。
 *
 * 采集员只来自飞书登录；`capture.yml` 仅提供任务等业务分类。业务选项写操作是**配置级**动作
 * （改设备上的配置文件），须持租约。
 */
export function MetaCard() {
    const { api, hasLease, nodeState, sessionType, captureStatus, onLog, authStatus, auth } = useEdge();
    const [options, setOptions] = useState<Record<string, string[]>>({});
    const [selection, setSelection] = useState<Record<string, string>>({});
    /** 正在新增 / 重命名的分类（`old` 非空 = 重命名） */
    const [editing, setEditing] = useState<{ key: string; old: string | null } | null>(null);
    const [draft, setDraft] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const inCapture = nodeState === "active" && sessionType === "capture";
    const canSync = nodeState === "ready" || inCapture;
    const writeEnabled = api !== null && hasLease && !busy;
    const keys = useMemo(() => orderedKeys(options), [options]);
    const syncedMeta = useMemo(() => captureStatus?.meta ?? {}, [captureStatus?.meta]);
    const effectiveSelection = useMemo(
        () => (auth ? { ...selection, operator: auth.user.display_name } : selection),
        [auth, selection]
    );
    const missingSelection = useMemo(() => missingCaptureMeta(effectiveSelection), [effectiveSelection]);

    /** 写回执与 GET 同构（`{meta: 全量}`）：直接替换本地选项，清掉失效引用。 */
    const applyMeta = useCallback((next: Record<string, string[]>) => {
        setOptions(next);
        setSelection((prev) => {
            const out: Record<string, string> = {};
            for (const [key, options] of Object.entries(next)) {
                const previous = prev[key];
                if (previous && options.includes(previous)) out[key] = previous;
                else if (options.length > 0) out[key] = options[0];
            }
            return out;
        });
        setEditing((prev) => (prev && (prev.old === null || next[prev.key]?.includes(prev.old)) ? prev : null));
    }, []);

    const load = useCallback(async () => {
        if (!api) return;
        try {
            applyMeta((await api.captureMeta()).meta ?? {});
            setError(null);
        } catch (e) {
            setError(errMsg(e));
        }
    }, [api, applyMeta]);

    useEffect(() => {
        void load();
    }, [load]);

    /** 写操作统一出口：成功回执即新选项集，失败提示原因。 */
    const write = async (label: string, fn: () => Promise<CaptureMetaResponse>, after?: () => void) => {
        setBusy(true);
        setError(null);
        try {
            applyMeta((await fn()).meta ?? {});
            after?.();
            onLog(`${label} 成功`, true);
        } catch (e) {
            const msg = errMsg(e);
            setError(msg);
            onLog(`${label} 失败：${msg}`, false);
        } finally {
            setBusy(false);
        }
    };

    /** 提交选中的元信息到机器人进程（`capture sync`：只在采集会话内被接受）。 */
    const syncMeta = async () => {
        if (!api) return;
        // 请求也必须使用与必填校验相同的 effectiveSelection；服务端仍会用可信身份覆盖 operator。
        const meta = captureSyncMeta(effectiveSelection);
        setBusy(true);
        setError(null);
        try {
            await api.capturesSync(meta);
            onLog(`同步元信息 成功：${JSON.stringify(meta)}`, true);
        } catch (e) {
            setError(errMsg(e));
        } finally {
            setBusy(false);
        }
    };

    // 无分类时给一个空壳，方便直接新建第一类（分类名写在上面那行的输入框里）
    const rows = (keys.length > 0 ? keys : []).filter((key) => key !== "operator");

    return (
        <Card
            title="Meta"
            actions={
                <>
                    <Button
                        size="xs"
                        disabled={busy || !api || !canSync || !hasLease || missingSelection.length > 0}
                        title={
                            !canSync
                                ? "机器人须处于 READY 或采集会话中"
                                : !hasLease
                                ? "同步需要活跃租约"
                                : missingSelection.length > 0
                                ? `请先选择${missingSelection.join("、")}`
                                : "同步选中的元信息到机器人进程（可在进入会话前完成）"
                        }
                        onClick={() => void syncMeta()}
                    >
                        同步
                    </Button>
                    <Button
                        size="xs"
                        variant="ghost"
                        disabled={busy || !api}
                        title="重新读取选项"
                        onClick={() => void load()}
                    >
                        ↻
                    </Button>
                </>
            }
        >
            <div className="space-y-1.5">
                <div className="flex items-center gap-1">
                    <span className="w-[3.6rem] shrink-0 text-[10px] text-faint">采集人 *</span>
                    {auth ? (
                        <span className="min-w-0 flex-1 truncate rounded border border-line bg-panel2 px-2 py-1 text-[11px] text-ink">
                            {auth.user.display_name}
                        </span>
                    ) : authStatus?.enabled ? (
                        <Button
                            size="xs"
                            variant="primary"
                            className="min-w-0 flex-1"
                            onClick={() => window.location.assign(api!.authLoginUrl(window.location.pathname))}
                        >
                            使用飞书登录
                        </Button>
                    ) : (
                        <span className="min-w-0 flex-1 rounded border border-amber-700 bg-amber-950/30 px-2 py-1 text-[11px] text-amber-400">
                            Edge 未启用飞书认证
                        </span>
                    )}
                </div>
                {rows.map((key) => {
                    const isEditing = editing?.key === key;
                    const value = selection[key] ?? "";
                    return (
                        <div key={key} className="flex items-center gap-1">
                            <span className="w-[3.6rem] shrink-0 truncate text-[10px] text-faint" title={key}>
                                {LABELS[key] ?? key}
                                {REQUIRED_CAPTURE_META.some((item) => item.key === key) ? " *" : ""}
                            </span>
                            {isEditing ? (
                                <>
                                    <input
                                        className={inputCls}
                                        autoFocus
                                        value={draft}
                                        placeholder={editing.old ? "新名称" : "新选项"}
                                        onChange={(e) => setDraft(e.target.value)}
                                        onKeyDown={(e) => {
                                            if (e.key === "Escape") setEditing(null);
                                            if (e.key !== "Enter" || draft.trim() === "") return;
                                            const old = editing.old;
                                            void write(
                                                old ? `重命名 ${key}` : `新增 ${key}`,
                                                () =>
                                                    old
                                                        ? api!.captureMetaEdit(key, old, draft.trim())
                                                        : api!.captureMetaAdd(key, draft.trim()),
                                                () => {
                                                    setSelection((prev) => ({ ...prev, [key]: draft.trim() }));
                                                    setEditing(null);
                                                }
                                            );
                                        }}
                                    />
                                    <button
                                        type="button"
                                        className={iconBtn}
                                        disabled={busy || draft.trim() === ""}
                                        title="保存"
                                        onClick={() => {
                                            const old = editing.old;
                                            void write(
                                                old ? `重命名 ${key}` : `新增 ${key}`,
                                                () =>
                                                    old
                                                        ? api!.captureMetaEdit(key, old, draft.trim())
                                                        : api!.captureMetaAdd(key, draft.trim()),
                                                () => {
                                                    setSelection((prev) => ({ ...prev, [key]: draft.trim() }));
                                                    setEditing(null);
                                                }
                                            );
                                        }}
                                    >
                                        ✓
                                    </button>
                                    <button
                                        type="button"
                                        className={iconBtn}
                                        title="取消"
                                        onClick={() => setEditing(null)}
                                    >
                                        ✕
                                    </button>
                                </>
                            ) : (
                                <>
                                    <select
                                        className={inputCls}
                                        value={value}
                                        disabled={!writeEnabled && !api}
                                        onChange={(e) => setSelection((prev) => ({ ...prev, [key]: e.target.value }))}
                                    >
                                        <option value="">—</option>
                                        {(options[key] ?? []).map((option) => (
                                            <option key={option} value={option}>
                                                {option}
                                            </option>
                                        ))}
                                    </select>
                                    <button
                                        type="button"
                                        className={iconBtn}
                                        disabled={!writeEnabled}
                                        title="新增选项"
                                        onClick={() => {
                                            setEditing({ key, old: null });
                                            setDraft("");
                                        }}
                                    >
                                        ＋
                                    </button>
                                    <button
                                        type="button"
                                        className={iconBtn}
                                        disabled={!writeEnabled || value === ""}
                                        title="重命名选中项"
                                        onClick={() => {
                                            setEditing({ key, old: value });
                                            setDraft(value);
                                        }}
                                    >
                                        ✎
                                    </button>
                                    <button
                                        type="button"
                                        className={iconBtn}
                                        disabled={!writeEnabled || value === ""}
                                        title="删除选中项"
                                        onClick={() =>
                                            void write(`删除 ${key}=${value}`, () => api!.captureMetaDelete(key, value))
                                        }
                                    >
                                        🗑
                                    </button>
                                </>
                            )}
                        </div>
                    );
                })}

                {rows.length === 0 && <p className="text-[11px] text-muted">无业务选项，请先配置采集任务</p>}
                {rows.length > 0 && (
                    <p className="text-[10px] text-faint">
                        任务候选来自 capture.yml；机器人 READY 后即可同步，无需先进入采集会话。
                    </p>
                )}

                {/* 已同步摘要：上面的分类选的是**待同步**的值（下拉 + 增删改），这里只读展示
                    机器人进程**当前持有**的那份 meta（4 项：时长 / 帧数 / 采集人 / 任务）。 */}
                <div className="space-y-0.5 border-t border-slate-800 pt-1.5">
                    <div className="text-[10px] text-slate-500">已同步（来自 Edge）</div>
                    {SYNCED_SUMMARY.map((item) => {
                        const value = statValue(syncedMeta, item);
                        return (
                            <div key={item.label} className="flex items-baseline gap-2">
                                <span className="w-[3.6rem] shrink-0 truncate text-[10px] text-faint">
                                    {item.label}
                                </span>
                                <span className="min-w-0 truncate font-mono text-[12px] text-slate-200" title={value}>
                                    {value}
                                </span>
                            </div>
                        );
                    })}
                </div>

                {!canSync && <p className="text-[10px] text-slate-500">机器人 READY 后即可提前同步</p>}
                {canSync && missingSelection.length > 0 && (
                    <p className="text-[10px] text-amber-400">开始采集前必填并同步：{missingSelection.join("、")}</p>
                )}
                {!hasLease && <p className="text-[10px] text-amber-400">需租约</p>}
                <ErrorText message={error} />
            </div>
        </Card>
    );
}
