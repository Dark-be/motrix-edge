import { useEffect, useMemo, useState } from "react";

import type { EdgeApi } from "../api";
import { errMsg, type UploadEpisode, type UploadStatus } from "../types";
import { Badge, BlockTitle, Button, brief, Card, Disclosure, ErrorText } from "./ui";

// ---- meta 字段格式化（JSON 描述文件解析出的结构化字段；缺省 → "—"）--------
const fmtBytes = (n: number | string | null | undefined): string => {
    const bytes = Number(n);
    if (!Number.isFinite(bytes) || bytes < 0) return "—";
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let v = bytes / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
    }
    return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
};

const fmtDuration = (n: number | string | null | undefined): string => {
    const v = Number(n);
    return Number.isFinite(v) ? `${v.toFixed(1)}s` : "—";
};

const fmtTime = (s: string | number | null | undefined): string => {
    if (s == null) return "—";
    const str = String(s);
    const m = str.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : str;
};

/** meta 值是否可展示（机器人进程会把未填字段写成 null / "None" 字符串）。 */
const shown = (v: unknown): string | null => {
    if (v === null || v === undefined) return null;
    const s = String(v).trim();
    return s === "" || s === "None" || s === "null" ? null : s;
};

const statusTone = (status: string): "green" | "red" | "amber" | "slate" =>
    status === "ready" || status === "succeeded"
        ? "green"
        : status === "invalid" || status === "failed"
        ? "red"
        : status === "pending" || status === "uploading"
        ? "amber"
        : "slate";

// ---- Episode 列表工具（搜索 / 状态筛选 / 排序 / 分页）----
const PAGE_SIZE = 10; // 每页渲染条数（几十上百个 episode 时分页，避免全量 DOM）

type SortKey = "name" | "time" | "size" | "frames" | "duration";
const SORT_OPTIONS: { key: SortKey; label: string }[] = [
    { key: "time", label: "创建时间" },
    { key: "name", label: "名称" },
    { key: "size", label: "大小" },
    { key: "frames", label: "帧数" },
    { key: "duration", label: "时长" },
];

const STATUS_OPTIONS: string[] = ["all", "ready", "pending", "uploading", "succeeded", "failed", "invalid"];
const STATUS_LABELS: Record<string, string> = {
    all: "全部",
    ready: "可用",
    pending: "排队",
    uploading: "上传中",
    succeeded: "成功",
    failed: "失败",
    invalid: "异常",
};

export function UploadPanel({ api, onLog }: { api: EdgeApi; onLog: (msg: string, ok: boolean) => void }) {
    const [collapsed, setCollapsed] = useState(false);
    const [folderPath, setFolderPath] = useState("");
    const [status, setStatus] = useState<UploadStatus | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [lastResult, setLastResult] = useState<string | null>(null);
    // Episode 列表工具状态（搜索 / 状态筛选 / 排序 / 分页）
    const [query, setQuery] = useState("");
    const [statusFilter, setStatusFilter] = useState("all");
    const [sortKey, setSortKey] = useState<SortKey>("time");
    const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
    const [page, setPage] = useState(0);
    // 打包名（预填 pack<选中数量>；重名 → 后端 409，需改名）
    const [packName, setPackName] = useState("");

    const run = async (label: string, fn: () => Promise<unknown>) => {
        setBusy(true);
        setError(null);
        try {
            const next = (await fn()) as UploadStatus;
            setStatus(next);
            setLastResult(`${label} → ${JSON.stringify(next)}`);
            onLog(`${label} 成功: ${brief(next, 120)}`, true);
        } catch (e) {
            const message = errMsg(e);
            setError(message);
            setLastResult(`${label} 失败`);
            onLog(`${label} 失败: ${message}`, false);
        } finally {
            setBusy(false);
        }
    };

    // 「获取数据目录」：读 GET /v1/captures 的 capture_status.data_dir（进程报告的数据目录），
    // 填入目录输入框；仅按钮触发，不做自动填充（capture / upload 各自独立操作）。
    const fetchDataDir = () => {
        if (!api) return;
        setBusy(true);
        setError(null);
        api.capturesStatus()
            .then((s) => {
                const dir = s.capture_status?.data_dir ?? null;
                if (dir) {
                    setFolderPath(dir);
                    onLog(`获取数据目录 → ${dir}`, true);
                } else {
                    setError("未获取到数据目录（adapter 未报告 data_dir）");
                    onLog("获取数据目录失败: adapter 未报告 data_dir", false);
                }
            })
            .catch((e) => {
                const msg = errMsg(e);
                setError(msg);
                onLog(`获取数据目录失败: ${msg}`, false);
            })
            .finally(() => setBusy(false));
    };

    const selectedIds =
        status?.episodes.filter((episode) => episode.selected).map((episode) => episode.episode_id) ?? [];
    const toggle = (episodeId: string, selected: boolean) => {
        const next = selected ? [...selectedIds, episodeId] : selectedIds.filter((id) => id !== episodeId);
        void run("更新选择", () => api.selectUploads(next));
    };

    // ---- 筛选 + 排序 + 分页（本地派生，只渲染当前页）----
    const episodes = status?.episodes ?? [];
    const countByStatus = useMemo(() => {
        const counts: Record<string, number> = {};
        for (const ep of episodes) counts[ep.status] = (counts[ep.status] ?? 0) + 1;
        return counts;
    }, [episodes]);

    const filtered = useMemo(() => {
        const q = query.trim().toLowerCase();
        return episodes.filter((ep) => {
            if (statusFilter !== "all" && ep.status !== statusFilter) return false;
            if (!q) return true;
            const meta = ep.meta ?? {};
            const haystack = [ep.episode_id, meta.robot_name, meta.robot_type, meta.operator, meta.task_name]
                .filter(Boolean)
                .join(" ")
                .toLowerCase();
            return haystack.includes(q);
        });
    }, [episodes, query, statusFilter]);

    const sorted = useMemo(() => {
        const valueOf = (ep: UploadEpisode): string | number => {
            const meta = ep.meta ?? {};
            switch (sortKey) {
                case "size":
                    return Number(meta.size_bytes) || 0;
                case "frames":
                    return Number(meta.frames) || 0;
                case "duration":
                    return Number(meta.duration) || 0;
                case "name":
                    return ep.episode_id;
                case "time":
                default:
                    return meta.created_at ? String(meta.created_at) : ep.episode_id;
            }
        };
        const next = [...filtered].sort((a, b) => {
            const va = valueOf(a);
            const vb = valueOf(b);
            const cmp =
                typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
            return sortDir === "asc" ? cmp : -cmp;
        });
        return next;
    }, [filtered, sortKey, sortDir]);

    const pageCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
    const pageEpisodes = sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
    // 搜索 / 筛选 / 排序变化时回到第 1 页
    useEffect(() => setPage(0), [query, statusFilter, sortKey, sortDir]);

    // 批量选择：全选当前筛选结果中可选的 episode / 清空选择集
    const selectAllFiltered = () => {
        const ids = filtered.filter((ep) => ep.status !== "invalid").map((ep) => ep.episode_id);
        void run("全选当前筛选", () => api.selectUploads([...new Set([...selectedIds, ...ids])]));
    };
    const clearSelection = () => {
        void run("清空选择", () => api.selectUploads([]));
    };

    const canUpload = !busy && selectedIds.length > 0 && !!status?.endpoint_configured;
    // 包名预填：后端建议 pack<选中数量>（选择集变化时跟随）
    useEffect(() => {
        if (status?.suggested_pack_name) setPackName(status.suggested_pack_name);
    }, [status?.suggested_pack_name]);
    // 打包：选中非空 + 包名非空即可（不需上传目标）
    const canPack = !busy && selectedIds.length > 0 && packName.trim() !== "";

    // 打包（移动）选中 episode：回执带回重扫结果，直接替换列表
    const packSelected = async () => {
        const name = packName.trim();
        if (name === "") {
            setError("请填写包名");
            return;
        }
        setBusy(true);
        setError(null);
        try {
            const result = await api.packUploads(name);
            setStatus(result.scan);
            setPackName("");
            setLastResult(`打包 → ${result.path}（${result.episode_count} episode / ${result.file_count} 文件）`);
            onLog(`打包成功 → ${result.path}`, true);
        } catch (e) {
            const message = errMsg(e);
            setError(message);
            setLastResult("打包失败");
            onLog(`打包失败: ${message}`, false);
        } finally {
            setBusy(false);
        }
    };

    return (
        <Card title="Upload" collapsible collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)}>
            <div className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-slate-500">
                {status ? (
                    <>
                        <span>
                            Episode <span className="font-mono text-slate-200">{status.episode_count}</span>
                        </span>
                        <span>
                            可用 <span className="font-mono text-emerald-300">{status.ready_count}</span>
                        </span>
                        <span>
                            异常{" "}
                            <span
                                className={
                                    status.invalid_count > 0 ? "font-mono text-rose-300" : "font-mono text-slate-200"
                                }
                            >
                                {status.invalid_count}
                            </span>
                        </span>
                    </>
                ) : (
                    <span>尚未扫描目录</span>
                )}
            </div>

            {/* 扫描目录：输入目录 → 扫描（POST /v1/uploads）；留空用回退链（adapter 数据目录 → upload.data_dir） */}
            <div className="space-y-1.5">
                <label className="flex min-w-0 items-center gap-2">
                    <span className="w-[4.5rem] shrink-0 truncate text-[10px] text-slate-500">数据目录</span>
                    <input
                        value={folderPath}
                        onChange={(e) => setFolderPath(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === "Enter" && !busy) {
                                void run("扫描采集目录", () => api.scanUploads(folderPath.trim() || undefined));
                            }
                        }}
                        placeholder="留空使用 adapter 数据目录 / upload.data_dir"
                        disabled={busy}
                        title="留空时依次回退：adapter 数据目录（机器人数据文件存放路径）→ upload.data_dir 配置"
                        className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 placeholder:text-slate-600 disabled:opacity-50"
                    />
                </label>
                <div className="grid grid-cols-2 gap-1.5">
                    <Button
                        size="xs"
                        disabled={busy}
                        title="请求 GET /v1/captures，把 adapter 报告的数据目录（data_dir）填入输入框"
                        onClick={fetchDataDir}
                    >
                        获取数据目录
                    </Button>
                    <Button
                        size="xs"
                        variant="primary"
                        disabled={busy}
                        onClick={() => void run("扫描采集目录", () => api.scanUploads(folderPath.trim() || undefined))}
                    >
                        扫描目录
                    </Button>
                </div>
            </div>

            {status && (
                <>
                    <div className="mt-2">
                        <BlockTitle
                            actions={
                                <span
                                    className="min-w-0 max-w-[60%] truncate font-mono text-[10px] text-slate-500"
                                    title={status.folder_path ?? ""}
                                >
                                    {status.folder_path ?? "—"}
                                </span>
                            }
                        >
                            Episode 列表（显示 {sorted.length} / 共 {status.episodes.length}）
                        </BlockTitle>

                        {/* 工具条：搜索 / 状态筛选 / 排序 / 批量选择 */}
                        <div className="mb-1.5 space-y-1.5">
                            <input
                                value={query}
                                onChange={(e) => setQuery(e.target.value)}
                                placeholder="搜索 episode / 机器人 / 采集员 / 任务"
                                disabled={busy}
                                className="w-full min-w-0 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 placeholder:text-slate-600 disabled:opacity-50"
                            />
                            <div className="grid grid-cols-2 gap-1.5">
                                <select
                                    value={statusFilter}
                                    onChange={(e) => setStatusFilter(e.target.value)}
                                    disabled={busy}
                                    title="按状态筛选"
                                    className="rounded border border-slate-700 bg-slate-950 px-1.5 py-1 text-[11px] text-slate-100 disabled:opacity-50"
                                >
                                    {STATUS_OPTIONS.map((s) => (
                                        <option key={s} value={s}>
                                            {STATUS_LABELS[s]}（
                                            {s === "all" ? status.episodes.length : countByStatus[s] ?? 0}）
                                        </option>
                                    ))}
                                </select>
                                <select
                                    value={sortKey}
                                    onChange={(e) => setSortKey(e.target.value as SortKey)}
                                    disabled={busy}
                                    title="排序字段"
                                    className="rounded border border-slate-700 bg-slate-950 px-1.5 py-1 text-[11px] text-slate-100 disabled:opacity-50"
                                >
                                    {SORT_OPTIONS.map((o) => (
                                        <option key={o.key} value={o.key}>
                                            {o.label}
                                        </option>
                                    ))}
                                </select>
                            </div>
                            <div className="grid grid-cols-3 gap-1.5">
                                <Button
                                    size="xs"
                                    disabled={busy}
                                    title="切换升 / 降序"
                                    onClick={() => setSortDir((d) => (d === "asc" ? "desc" : "asc"))}
                                >
                                    {sortDir === "asc" ? "升序 ↑" : "降序 ↓"}
                                </Button>
                                <Button
                                    size="xs"
                                    disabled={busy}
                                    title="全选当前筛选结果中可用的 episode"
                                    onClick={selectAllFiltered}
                                >
                                    全选筛选
                                </Button>
                                <Button size="xs" disabled={busy} title="清空选择集" onClick={clearSelection}>
                                    清空
                                </Button>
                            </div>
                        </div>

                        {/* 列表：仅渲染当前页（几十上百个 episode 时避免全量 DOM） */}
                        <div className="max-h-96 space-y-1 overflow-auto">
                            {pageEpisodes.length === 0 ? (
                                <p className="py-2 text-center text-[11px] text-slate-500">
                                    无匹配 episode（调整搜索 / 筛选）。
                                </p>
                            ) : (
                                pageEpisodes.map((episode) => {
                                    const meta = episode.meta ?? {};
                                    const invalid = episode.status === "invalid";
                                    // 指标行：帧 / 时长 / 大小 / 时间（sha256 进 title，不占版面）
                                    const metrics = [
                                        meta.frames != null && `${meta.frames} 帧`,
                                        meta.duration != null && fmtDuration(meta.duration),
                                        meta.size_bytes != null && fmtBytes(meta.size_bytes),
                                        meta.created_at && fmtTime(meta.created_at),
                                    ]
                                        .filter(Boolean)
                                        .join(" · ");
                                    return (
                                        <label
                                            key={episode.episode_id}
                                            className={`flex cursor-pointer items-start gap-2 rounded border px-2 py-1 text-[11px] transition-colors ${
                                                invalid
                                                    ? "border-rose-900/50 bg-rose-950/20 opacity-70"
                                                    : "border-slate-800 bg-slate-950/30 hover:border-slate-600"
                                            }`}
                                        >
                                            <input
                                                type="checkbox"
                                                checked={episode.selected}
                                                disabled={busy || invalid}
                                                onChange={(e) => toggle(episode.episode_id, e.target.checked)}
                                                className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-sky-600 disabled:opacity-40"
                                            />
                                            <span className="min-w-0 flex-1">
                                                {/* 标题行：episode_id + 状态 + 机器人 + 采集人 / 任务 */}
                                                <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                                                    <span
                                                        className="font-mono text-slate-100"
                                                        title={episode.episode_id}
                                                    >
                                                        {episode.episode_id}
                                                    </span>
                                                    <Badge tone={statusTone(episode.status)}>{episode.status}</Badge>
                                                    {meta.robot_name && (
                                                        <span className="truncate text-slate-300">
                                                            {meta.robot_name}
                                                            {meta.robot_type ? ` (${meta.robot_type})` : ""}
                                                        </span>
                                                    )}
                                                    {/* 采集人 / 任务（都为空则不占位：机器人进程写的是 null / None） */}
                                                    {(shown(meta.operator) || shown(meta.task_name)) && (
                                                        <span className="truncate text-slate-400">
                                                            {shown(meta.operator) ?? "—"} ·{" "}
                                                            {shown(meta.task_name) ?? "—"}
                                                        </span>
                                                    )}
                                                </span>
                                                {/* 指标行：帧 / 时长 / 大小 / 时间（sha256 悬浮可见） */}
                                                <span
                                                    className="mt-0.5 block truncate font-mono text-[10px] text-slate-500"
                                                    title={meta.sha256 ? `sha256 ${meta.sha256}` : undefined}
                                                >
                                                    {metrics || "—"}
                                                </span>
                                                {episode.errors.length > 0 && (
                                                    <span className="mt-0.5 block text-rose-300">
                                                        {episode.errors.join("；")}
                                                    </span>
                                                )}
                                            </span>
                                        </label>
                                    );
                                })
                            )}
                        </div>

                        {/* 分页 + 选择摘要 */}
                        <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2">
                            <span className="text-[10px] text-slate-500">
                                已选 {selectedIds.length} · 第 {page + 1}/{pageCount} 页 · 每页 {PAGE_SIZE}
                            </span>
                            <div className="flex items-center gap-1.5">
                                <Button
                                    size="xs"
                                    disabled={busy || page <= 0}
                                    onClick={() => setPage((p) => Math.max(0, p - 1))}
                                >
                                    上一页
                                </Button>
                                <Button
                                    size="xs"
                                    disabled={busy || page >= pageCount - 1}
                                    onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                                >
                                    下一页
                                </Button>
                            </div>
                        </div>
                    </div>
                </>
            )}

            {/* 选择完成后再处理：本地打包是当前主流程，远端上传是可选能力。 */}
            {status && (
                <div className="mt-2 space-y-1.5 border-t border-line pt-2">
                    <BlockTitle
                        actions={
                            <Badge tone={selectedIds.length > 0 ? "blue" : "slate"}>已选 {selectedIds.length}</Badge>
                        }
                    >
                        处理选中项
                    </BlockTitle>
                    <label className="flex min-w-0 items-center gap-2">
                        <span className="w-[4.5rem] shrink-0 text-[10px] text-slate-500">打包名称</span>
                        <input
                            value={packName}
                            onChange={(e) => setPackName(e.target.value)}
                            onKeyDown={(e) => {
                                if (e.key === "Enter" && canPack) void packSelected();
                            }}
                            placeholder="如 pack10"
                            disabled={busy}
                            title="把选中的 .mcap + .json 移动到「扫描目录/包名/」；同名目录已存在时请改名"
                            className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 placeholder:text-slate-600 disabled:opacity-50"
                        />
                    </label>
                    <Button
                        className="w-full"
                        variant="primary"
                        disabled={!canPack}
                        onClick={() => void packSelected()}
                    >
                        打包选中（{selectedIds.length}）
                    </Button>
                    <Disclosure
                        label="远端上传"
                        actions={
                            <Badge tone={status.endpoint_configured ? "green" : "amber"}>
                                {status.endpoint_configured ? "已配置" : "未配置"}
                            </Badge>
                        }
                    >
                        <div className="grid grid-cols-2 gap-1.5">
                            <Button
                                variant="primary"
                                disabled={!canUpload}
                                title={status.endpoint_configured ? "POST /v1/uploads/upload" : "未配置远端上传目标"}
                                onClick={() => void run("上传选中", () => api.enqueueUploads())}
                            >
                                上传选中
                            </Button>
                            <Button
                                disabled={!canUpload}
                                title="POST /v1/uploads/retry"
                                onClick={() => void run("重试失败", () => api.retryUploads())}
                            >
                                重试失败
                            </Button>
                        </div>
                    </Disclosure>
                </div>
            )}

            {lastResult && (
                <div className="mt-2">
                    <Disclosure label="最近结果">
                        <pre className="max-h-20 overflow-auto font-mono text-[10px] text-emerald-300">
                            {lastResult}
                        </pre>
                    </Disclosure>
                </div>
            )}
            <ErrorText message={error} />
        </Card>
    );
}
