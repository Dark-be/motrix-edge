import { useCallback, useEffect, useState } from "react";

import type { EdgeApi } from "../api";
import { errMsg, type RpentEnvelope, type RpentInfoResponse } from "../types";
import { Badge, Button, Card, Disclosure, ErrorText, Field, FieldGrid } from "./ui";

const inputCls =
    "rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 disabled:opacity-50";

/** 选方法时自动填的 kwargs 模板（值都是真实契约，减少手写拼错）。 */
const TEMPLATES: Record<string, string> = {
    "env.get_object_position": '{\n  "camera": "cam_head",\n  "x": 0.5,\n  "y": 0.5\n}',
    "env.look_at": '{\n  "arm": "left",\n  "target": [0.3, 0.1, 0.35],\n  "keep": "target",\n  "settle": false\n}',
    "env.move_to":
        '{\n  "arm": "left",\n  "target": [0.3, 0.1, 0.35],\n  "rpy": [0.0, 0.0, 0.0],\n  "settle": false\n}',
    "env.move_delta": '{\n  "arm": "left",\n  "delta_xyz": [0.02, 0.0, 0.0],\n  "settle": false\n}',
    "env.rotate_delta": '{\n  "arm": "left",\n  "delta_rpy": [0.0, 0.0, 0.05],\n  "settle": false\n}',
    "env.set_gripper": '{\n  "arm": "right",\n  "open": true,\n  "settle": false\n}',
    "env.recover_joint_posture": '{\n  "settle": false\n}',
};

function asRecord(value: unknown): Record<string, unknown> {
    return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/** `[x, y, z]`（米）→ 三小数文本；缺失 → `"—"`（不编造 `0`）。 */
function vecText(value: unknown): string {
    if (!Array.isArray(value) || value.length < 3) return "—";
    return `(${value
        .slice(0, 3)
        .map((n) => Number(n).toFixed(3))
        .join(", ")})`;
}

function numOrNull(value: unknown): number | null {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** `env.get_object_position` 的失败分类 → 现场能看懂的一句话（只说能确定的那种）。 */
function positionHint(kind: string | null): string | null {
    if (kind === "no_depth") return "该像素没有有效深度（深度原始值 0）——换一个点或换一台相机。";
    if (kind === "uncalibrated") return "该相机没有可用外参（未标定 / 产物里没有它）——连相机系坐标也给不出。";
    if (kind === "no_pose")
        return "腕相机的 `world` 坐标需要**同一拍**的臂位姿（observations.pose）；这一拍没有——先让臂停稳再查。";
    return kind ? `不可用（${kind}）` : null;
}

/**
 * RPent 试验台（`POST /call`）：**一个卡片两件事**——
 *
 * 1. **任意方法直呼**：method 下拉（来自 `GET /v1/rpent` 的 `methods`）+ kwargs JSON → 回执原样渲染。
 *    RPent 面**永远 HTTP 200**，失败在信封里（`ok=false` + `error` / `kind`），故卡片把两类都显示出来。
 * 2. **像素 → 3D 坐标 + 指向**：归一化 `u` / `v` 调 `env.get_object_position`（与原生 `/v1/depth` 同源），
 *    拿到 `xyz_world` 后可一键 `env.look_at` 指向该点——`world` 点**不需要**人工换算（左臂基座系）。
 *
 * 参考系（与 edge 侧一致，别混）：`move_delta` / `rotate_delta` 的增量在**末端（工具）系**；
 * `move_to` / `look_at` / `get_object_position` 吃 / 吐 **`world`**（左臂基座）。写方法要 Edge 级租约。
 */
export function RpentPanel({
    api,
    hasLease,
    onLog,
}: {
    api: EdgeApi | null;
    hasLease: boolean;
    onLog: (msg: string, ok: boolean) => void;
}) {
    const connected = api !== null;
    const [info, setInfo] = useState<RpentInfoResponse | null>(null);
    const [listError, setListError] = useState<string | null>(null);
    // 任意方法调用
    const [method, setMethod] = useState("env.get_env_meta");
    const [kwargs, setKwargs] = useState("{}");
    const [reply, setReply] = useState<RpentEnvelope | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    // 像素 → 坐标
    const [cameras, setCameras] = useState<string[]>([]);
    const [camera, setCamera] = useState("");
    const [u, setU] = useState("0.5");
    const [v, setV] = useState("0.5");
    const [arm, setArm] = useState<"left" | "right">("left");
    const [position, setPosition] = useState<RpentEnvelope | null>(null);
    const [posBusy, setPosBusy] = useState(false);
    const [posError, setPosError] = useState<string | null>(null);

    const loadInfo = useCallback(async () => {
        if (!api) return;
        try {
            const data = await api.rpentInfo();
            setInfo(data);
            setListError(null);
            setMethod((cur) => (cur && data.methods.includes(cur) ? cur : data.methods[0] ?? cur));
        } catch (e) {
            setInfo(null);
            setListError(errMsg(e));
        }
    }, [api]);

    // 可用深度相机：读 /v1/preview 的 observation.depth（与预览 / 深度同源，不额外引入契约）
    const loadCameras = useCallback(async () => {
        if (!api || !hasLease) return;
        try {
            const data = await api.preview();
            const names = data.observation.depth ?? [];
            setCameras(names);
            setCamera((cur) => (cur && names.includes(cur) ? cur : names[0] ?? ""));
        } catch {
            setCameras([]);
            setCamera("");
        }
    }, [api, hasLease]);

    useEffect(() => {
        if (!connected) {
            setInfo(null);
            setCameras([]);
            setCamera("");
            setPosition(null);
            return;
        }
        void loadInfo();
        void loadCameras();
    }, [connected, loadInfo, loadCameras]);

    const methods = info?.methods ?? [];

    const call = useCallback(
        async (target: string, payload: Record<string, unknown>): Promise<RpentEnvelope | null> => {
            if (!api) return null;
            try {
                return await api.rpentCall(target, payload);
            } catch (e) {
                const msg = errMsg(e);
                setError(`请求失败：${msg}`);
                onLog(`RPent ${target} 请求失败：${msg}`, false);
                return null;
            }
        },
        [api, onLog]
    );

    const send = useCallback(async () => {
        let payload: Record<string, unknown>;
        try {
            const raw = kwargs.trim();
            const parsed: unknown = raw === "" ? {} : JSON.parse(raw);
            if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
                throw new Error("必须是 JSON 对象");
            }
            payload = parsed as Record<string, unknown>;
        } catch (e) {
            setError(`kwargs 不是合法 JSON 对象：${errMsg(e)}`);
            setReply(null);
            return;
        }
        setBusy(true);
        setError(null);
        const body = await call(method, payload);
        if (body) {
            setReply(body);
            const ok = body.ok === true;
            onLog(
                ok ? `RPent ${method} ok` : `RPent ${method} 失败（${body.kind ?? "error"}）：${body.error ?? ""}`,
                ok
            );
        }
        setBusy(false);
    }, [call, kwargs, method, onLog]);

    const queryPosition = useCallback(async () => {
        if (!api || !camera) return;
        const un = Number(u);
        const vn = Number(v);
        if (u.trim() === "" || v.trim() === "" || !Number.isFinite(un) || !Number.isFinite(vn)) {
            setPosError("u / v 必须是数字");
            setPosition(null);
            return;
        }
        if (un < 0 || un > 1 || vn < 0 || vn > 1) {
            setPosError("u / v 必须是 [0, 1] 的归一化坐标（0.5 = 画面中心）");
            setPosition(null);
            return;
        }
        setPosBusy(true);
        setPosError(null);
        const body = await call("env.get_object_position", { camera, x: un, y: vn });
        if (body) {
            setPosition(body);
            const result = asRecord(body.result);
            const ok = body.ok === true && result.available === true;
            onLog(
                ok
                    ? `get_object_position ${camera} @(${result.u_px},${result.v_px}) = depth ${
                          result.depth_m
                      } m · world ${vecText(result.xyz_world)}`
                    : `get_object_position 不可用（${String(result.kind ?? body.kind ?? "error")}）：${String(
                          result.reason ?? body.error ?? ""
                      )}`,
                ok
            );
        }
        setPosBusy(false);
    }, [api, camera, u, v, call, onLog]);

    const lookAtPosition = useCallback(async () => {
        const result = asRecord(position?.result);
        const target = result.xyz_world;
        if (!Array.isArray(target) || target.length < 3) return;
        setPosBusy(true);
        setPosError(null);
        const body = await call("env.look_at", {
            arm,
            target: target.slice(0, 3),
            keep: "target",
            settle: false,
        });
        if (body) {
            const payload = asRecord(body.result);
            const ok = body.ok === true;
            onLog(
                ok
                    ? `look_at ${arm} → world ${vecText(target)}（转 ${
                          (payload.turned_deg as number[] | undefined)?.map((d) => d.toFixed(1)).join(", ") ?? "?"
                      }°）`
                    : `look_at 失败（${body.kind ?? "error"}）：${body.error ?? ""}`,
                ok
            );
            setReply(body); // 回执也进「方法调用」那块，字段（turned_deg / base_source / target_base）可细看
        }
        setPosBusy(false);
    }, [position, arm, call, onLog]);

    const canQuery = connected && hasLease;
    const positionResult = asRecord(position?.result);
    const available = position?.ok === true && positionResult.available === true;
    const hint = position ? positionHint(String(positionResult.kind ?? "") || null) : null;

    return (
        <Card
            title="Primitives"
            actions={
                <>
                    <Badge tone={info?.enabled ? "green" : "slate"}>
                        {info ? `${methods.length} 个方法` : listError ? "未启用" : "读取中…"}
                    </Badge>
                    <Button
                        size="xs"
                        onClick={() => void loadInfo()}
                        disabled={!connected}
                        title="重新读取 GET /v1/rpent"
                    >
                        刷新方法
                    </Button>
                </>
            }
        >
            {listError && <ErrorText message={`读取 /v1/rpent 失败：${listError}`} />}

            {/* 高频入口：像素 → 3D 坐标（env.get_object_position）+ 一键指向（env.look_at）。 */}
            <div className="space-y-1.5">
                <div className="flex items-baseline justify-between gap-2">
                    <h3 className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">像素定位</h3>
                    <span className="text-[10px] text-slate-500">归一化坐标 · world = 左臂基座</span>
                </div>
                <label className="flex min-w-0 items-center gap-2" htmlFor="rpent-camera">
                    <span className="w-[4.5rem] shrink-0 text-[10px] text-slate-500">深度相机</span>
                    <div className="min-w-0 flex-1">
                        {/* 可输可点：候选来自 /v1/preview 的 observation.depth，但机器人没上报时也能手输相机名 */}
                        <input
                            id="rpent-camera"
                            list="rpent-camera-options"
                            value={camera}
                            onChange={(e) => setCamera(e.target.value)}
                            disabled={!canQuery || posBusy}
                            placeholder={cameras.length === 0 ? "（无可用深度相机——可直接输相机名）" : "cam_head"}
                            className={`${inputCls} w-full`}
                        />
                        <datalist id="rpent-camera-options">
                            {cameras.map((name) => (
                                <option key={name} value={name} />
                            ))}
                        </datalist>
                    </div>
                </label>
                <div className="flex min-w-0 items-center gap-2">
                    <span className="w-[4.5rem] shrink-0 text-[10px] text-slate-500">坐标 u / v</span>
                    <label className="min-w-0 flex-1" htmlFor="rpent-u">
                        <input
                            id="rpent-u"
                            value={u}
                            onChange={(e) => setU(e.target.value)}
                            disabled={!canQuery || posBusy}
                            className={`${inputCls} w-full`}
                            aria-label="归一化坐标 u"
                            placeholder="u · 0–1"
                        />
                    </label>
                    <label className="min-w-0 flex-1" htmlFor="rpent-v">
                        <input
                            id="rpent-v"
                            value={v}
                            onChange={(e) => setV(e.target.value)}
                            disabled={!canQuery || posBusy}
                            className={`${inputCls} w-full`}
                            aria-label="归一化坐标 v"
                            placeholder="v · 0–1"
                        />
                    </label>
                </div>
                <Button
                    variant="primary"
                    className="w-full"
                    onClick={() => void queryPosition()}
                    disabled={!canQuery || posBusy || !camera}
                    title="env.get_object_position（与原生 GET /v1/depth 同一份实现）"
                >
                    {posBusy ? "查询中…" : "查询坐标"}
                </Button>
                <ErrorText message={posError} />
                {position && (
                    <div className="border-t border-line pt-1.5">
                        <div className="mb-1 flex items-center gap-2">
                            <Badge tone={available ? "green" : "amber"}>
                                {available ? "3D 可用" : String(positionResult.kind ?? "不可用") || "不可用"}
                            </Badge>
                            <span className="font-mono text-[10px] text-slate-500">
                                pixel {String(positionResult.u_px ?? "—")} / {String(positionResult.v_px ?? "—")}
                            </span>
                        </div>
                        <FieldGrid cols={1}>
                            <Field k="深度" v={`${numOrNull(positionResult.depth_m) ?? "—"} m`} />
                            <Field
                                k={`world（${String(positionResult.frame ?? "world")}${
                                    positionResult.world ? ` = ${positionResult.world}` : ""
                                }）`}
                                v={`${vecText(positionResult.xyz_world)} m`}
                            />
                            <Field k="camera（光学系）" v={`${vecText(positionResult.xyz_camera)} m`} />
                        </FieldGrid>
                        {hint && <p className="mt-1 text-[11px] text-amber-300">{hint}</p>}
                        <div className="mt-1.5 grid grid-cols-[1fr_2fr] gap-1.5">
                            <label className="min-w-0" htmlFor="rpent-look-arm">
                                <select
                                    id="rpent-look-arm"
                                    value={arm}
                                    onChange={(e) => setArm(e.target.value === "right" ? "right" : "left")}
                                    disabled={!canQuery || posBusy}
                                    className={`${inputCls} w-full disabled:opacity-50`}
                                    aria-label="指向臂"
                                >
                                    <option value="left">left</option>
                                    <option value="right">right</option>
                                </select>
                            </label>
                            <Button
                                onClick={() => void lookAtPosition()}
                                disabled={!canQuery || posBusy || !available}
                                title={
                                    available
                                        ? "env.look_at(target = 该点的 world 坐标)：位置不变、只改姿态"
                                        : "先查到可用的 world 坐标"
                                }
                            >
                                指向这个点
                            </Button>
                        </div>
                    </div>
                )}
            </div>

            {/* 低频诊断入口默认收起，避免 JSON 编辑器长期占满左栏。 */}
            <div className="mt-2 border-t border-line pt-2">
                <Disclosure label="高级方法调用" actions={<Badge tone="slate">{methods.length} 个方法</Badge>}>
                    <label className="flex min-w-0 items-center gap-2" htmlFor="rpent-method">
                        <span className="w-[3.5rem] shrink-0 text-[10px] text-slate-500">方法</span>
                        <select
                            id="rpent-method"
                            value={method}
                            onChange={(e) => {
                                setMethod(e.target.value);
                                setKwargs(TEMPLATES[e.target.value] ?? "{}");
                            }}
                            disabled={!connected || methods.length === 0}
                            className={`${inputCls} w-full disabled:opacity-50`}
                        >
                            {methods.length === 0 && <option value={method}>{method}</option>}
                            {methods.map((name) => (
                                <option key={name} value={name}>
                                    {name}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="mt-1.5 flex flex-col gap-0.5" htmlFor="rpent-kwargs">
                        <span className="text-[10px] text-slate-500">参数（JSON）</span>
                        <textarea
                            id="rpent-kwargs"
                            value={kwargs}
                            onChange={(e) => setKwargs(e.target.value)}
                            rows={5}
                            spellCheck={false}
                            disabled={!connected || busy}
                            className={`${inputCls} w-full leading-5`}
                        />
                    </label>
                    <div className="mt-1.5 grid grid-cols-2 gap-1.5">
                        <Button
                            onClick={() => setKwargs(TEMPLATES[method] ?? "{}")}
                            disabled={!connected}
                            title="填该方法的参数模板（没有模板则清空为 {}）"
                        >
                            填充模板
                        </Button>
                        <Button variant="primary" onClick={() => void send()} disabled={!canQuery || busy}>
                            {busy ? "调用中…" : "调用方法"}
                        </Button>
                    </div>
                    <ErrorText message={error} />
                    {reply && (
                        <div className="mt-2">
                            <div className="mb-1 flex items-center gap-2">
                                <Badge tone={reply.ok ? "green" : "red"}>
                                    {reply.ok ? "ok" : `失败 · ${reply.kind ?? "error"}`}
                                </Badge>
                                {!reply.ok && <span className="text-[11px] text-rose-300">{reply.error}</span>}
                            </div>
                            <pre className="max-h-72 overflow-auto rounded border border-slate-800 bg-slate-950/60 p-2 font-mono text-[10px] leading-4 text-slate-300">
                                {JSON.stringify(reply, null, 2)}
                            </pre>
                        </div>
                    )}
                </Disclosure>
            </div>
        </Card>
    );
}
