import { useEffect, useState } from "react";

import type { EdgeApi } from "../api";
import type { RtcStatus } from "../types";
import { Badge, Button, Card, KeyValue, KvGrid } from "./ui";

const AGGREGATE_FNS = ["weighted_average", "conservative", "average", "latest_only", "continuous"];
const inputCls =
    "rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 disabled:opacity-50";

/** 展开参数按钮的悬浮说明（原理类文档不进正文，避免挤占版面）。 */
const RTC_HELP =
    "每次推理取模型输出块的前 H 步，按「前置 P（跳过）/ 执行 E / 后置 S」切分；" +
    "请求时机 = 执行段还剩 P 步。模型输出块比 H 短时（如 openpi 16 步 vs H=50），三段与请求提前量按实际块长 / H 等比缩放。";

/** 后端字段可能缺失（Edge 进程是旧代码）：统一转表单文本，缺失 → 空串（不显示 undefined）。 */
const rtcField = (value: number | null | undefined): string =>
    typeof value === "number" && Number.isFinite(value) ? String(value) : "";

/**
 * RTC（实时动作块）卡片：块长 H / 前置 P / 执行 E / 后置 S 的编辑与运行状态。
 *
 * 默认**收起**（高级参数，日常推理用不到）；分段校验（`P + E + S = H`、`E > P`）在前端先兜一层，
 * 后端仍会再校验（`infer rtc set`）。
 */
export function RtcCard({
    api,
    hasLease,
    inInfer,
    busy,
    rtc,
    run,
    standalone = false,
}: {
    api: EdgeApi | null;
    hasLease: boolean;
    /** 是否处于推理会话（仅会话内可改 RTC 参数） */
    inInfer: boolean;
    busy: boolean;
    rtc: RtcStatus | null;
    run: (label: string, fn: () => Promise<unknown>) => Promise<void>;
    /** 推理页用顶层卡片，与 Policy / Infer 同级；旧调用仍可使用内嵌块。 */
    standalone?: boolean;
}) {
    const connected = api !== null;
    const [open, setOpen] = useState(false);
    const [horizon, setHorizon] = useState("");
    const [prefix, setPrefix] = useState("");
    const [suffix, setSuffix] = useState("");
    const [execution, setExecution] = useState("");
    const [aggregate, setAggregate] = useState("");
    const editable = connected && hasLease && inInfer && !busy;

    // 预填当前生效参数（只在用户尚未编辑该项时填，避免覆盖输入）
    useEffect(() => {
        const params = rtc?.params;
        if (!params) return;
        if (horizon === "") setHorizon(rtcField(params.action_horizon));
        if (prefix === "") setPrefix(rtcField(params.prefix_len));
        if (suffix === "") setSuffix(rtcField(params.suffix_len));
        if (execution === "") {
            // 后端未显式配置执行段时用推导值（P + E + S = H）预填，便于直接编辑
            const derived =
                params.execution_horizon != null
                    ? params.execution_horizon
                    : (params.action_horizon ?? 0) - (params.prefix_len ?? 0) - (params.suffix_len ?? 0);
            setExecution(derived > 0 ? String(derived) : "");
        }
        if (aggregate === "") setAggregate(params.aggregate_fn ?? "");
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [
        rtc?.params.enabled,
        rtc?.params.action_horizon,
        rtc?.params.prefix_len,
        rtc?.params.suffix_len,
        rtc?.params.execution_horizon,
        rtc?.params.aggregate_fn,
    ]);

    const num = (raw: string): number | null => {
        const text = raw.trim();
        if (text === "") return null;
        const value = Number(text);
        return Number.isInteger(value) && value >= 0 ? value : null;
    };
    const h = num(horizon);
    const p = num(prefix);
    const e = num(execution);
    const s = num(suffix);
    const total = (p ?? 0) + (e ?? 0) + (s ?? 0);
    const issue = (() => {
        if (h === null || p === null || e === null || s === null) {
            return "动作块长 H / 前置 P / 执行 E / 后置 S 都必须是非负整数";
        }
        if (h < 1) return "动作块长 H 必须 ≥ 1";
        if (p + e + s !== h) {
            return `三段之和 = ${p + e + s}，必须等于动作块长 H = ${h}（前置 + 执行 + 后置）`;
        }
        if (e <= p) return `执行段 E = ${e} 必须大于前置段 P = ${p}（否则推理期间会把执行段也吃掉）`;
        if (p + s >= h) return "前置 + 后置 必须小于动作块长 H（否则块内没有可执行段）";
        return null;
    })();
    const valid = issue === null;
    // 编辑 H / P / S 时自动把执行段补成「H - P - S」，保持三段之和恒等于 H
    const syncExecution = (nextH: string, nextP: string, nextS: string) => {
        const nh = num(nextH);
        const np = num(nextP);
        const ns = num(nextS);
        if (nh === null || np === null || ns === null) return;
        const derived = nh - np - ns;
        if (derived >= 1) setExecution(String(derived));
    };
    // 后端未上报的分段字段（旧进程 / 版本不匹配）→ 表单无法预填，给出明确提示
    // 模型块长 < 配置 H：后端把 P/E/S 与预取提前量按「实际块长 / H」等比缩放（见 wiki/design/motrix_edge_rtc.md），
    // 不提示的话面板上的「生效 H/P/E/S」与实际切分会对不上
    const blockLen = rtc?.last_chunk?.height ?? null;
    const scaledNote =
        rtc && blockLen !== null && blockLen < rtc.params.action_horizon
            ? `模型块长 ${blockLen} < H=${rtc.params.action_horizon}：分段与预取提前量已按 ${blockLen}/${rtc.params.action_horizon} 等比缩放（本次 E=${rtc.last_chunk?.lens.execution}、S=${rtc.last_chunk?.lens.suffix}）`
            : null;
    const missing = rtc
        ? (["action_horizon", "prefix_len", "execution_horizon", "suffix_len"] as const).filter(
              (key) => rtc.params?.[key] === undefined
          )
        : [];

    const apply = () =>
        run("应用 RTC 参数", () =>
            api!.setInferRtc({
                action_horizon: h,
                prefix_len: p,
                execution_horizon: e,
                suffix_len: s,
                aggregate_fn: aggregate.trim(),
            })
        );

    const content = (
        <>
            <div className="flex flex-wrap items-center justify-between gap-2">
                {!standalone && (
                    <h3 className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">RTC 参数</h3>
                )}
                <div className="flex flex-wrap items-center gap-1.5">
                    <Badge tone={rtc?.enabled ? "green" : "slate"}>
                        {rtc?.enabled ? "已启用" : rtc ? "已关闭" : "—"}
                    </Badge>
                    {rtc && rtc.index != null && rtc.remaining != null && (
                        <span className="font-mono text-[10px] text-slate-500">
                            步号 {rtc.index} / 剩余 {rtc.remaining}
                            {rtc.last_delay != null && ` · 耗时 ${rtc.last_delay}s ≈ ${rtc.last_delay_steps ?? 0} 步`}
                        </span>
                    )}
                    {rtc !== null && (
                        <Button
                            size="xs"
                            variant="ghost"
                            onClick={() => setOpen(!open)}
                            title={open ? "收起 RTC 参数" : `展开 RTC 参数（高级）：${RTC_HELP}`}
                        >
                            {open ? "收起参数" : "展开参数"}
                        </Button>
                    )}
                </div>
            </div>

            {/* 无 rtc（未注入 / Edge 旧代码）：不渲染空表单与校验报错 */}
            {rtc === null && <p className="mt-1 text-[11px] text-slate-500">无 RTC 参数</p>}

            {open && rtc !== null && (
                <>
                    {/* 分段预览条：按 H 的比例显示三段占比 */}
                    <div className="mt-2 flex h-4 overflow-hidden rounded border border-slate-800 bg-slate-900">
                        {valid && total > 0 ? (
                            <>
                                <div
                                    className="bg-rose-500/60"
                                    style={{ width: `${((p ?? 0) / total) * 100}%` }}
                                    title={`前置 P=${p}（跳过）`}
                                />
                                <div
                                    className="bg-sky-500/70"
                                    style={{ width: `${((e ?? 0) / total) * 100}%` }}
                                    title={`执行 E=${e}`}
                                />
                                <div
                                    className="bg-amber-500/60"
                                    style={{ width: `${((s ?? 0) / total) * 100}%` }}
                                    title={`后置 S=${s}`}
                                />
                            </>
                        ) : null}
                    </div>
                    <div className="mt-1 flex flex-wrap justify-between gap-2 text-[10px] text-slate-500">
                        <span>■ 前置 P={prefix || "—"}（跳过）</span>
                        <span>■ 执行 E={execution || "—"}（加权 + 直接执行）</span>
                        <span>■ 后置 S={suffix || "—"}（与下一块重叠）</span>
                    </div>

                    <div className="mt-2 space-y-1.5">
                        <label className="flex min-w-0 items-center gap-2">
                            <span
                                className="w-[5.5rem] shrink-0 truncate text-[10px] text-slate-500"
                                title="动作块长 H：每次推理截取模型输出块的前 H 步"
                            >
                                动作块长 H
                            </span>
                            <input
                                value={horizon}
                                onChange={(ev) => {
                                    setHorizon(ev.target.value);
                                    syncExecution(ev.target.value, prefix, suffix);
                                }}
                                disabled={!editable}
                                className={`${inputCls} min-w-0 flex-1`}
                            />
                        </label>
                        <label className="flex min-w-0 items-center gap-2">
                            <span
                                className="w-[5.5rem] shrink-0 truncate text-[10px] text-slate-500"
                                title="前置长度 P：块首跳过的步数"
                            >
                                前置 P
                            </span>
                            <input
                                value={prefix}
                                onChange={(ev) => {
                                    setPrefix(ev.target.value);
                                    syncExecution(horizon, ev.target.value, suffix);
                                }}
                                disabled={!editable}
                                className={`${inputCls} min-w-0 flex-1`}
                            />
                        </label>
                        <label className="flex min-w-0 items-center gap-2">
                            <span
                                className="w-[5.5rem] shrink-0 truncate text-[10px] text-slate-500"
                                title="执行长度 E：实际执行的步数，须大于前置段 P"
                            >
                                执行 E
                            </span>
                            <input
                                value={execution}
                                onChange={(ev) => setExecution(ev.target.value)}
                                disabled={!editable}
                                className={`${inputCls} min-w-0 flex-1`}
                            />
                        </label>
                        <label className="flex min-w-0 items-center gap-2">
                            <span
                                className="w-[5.5rem] shrink-0 truncate text-[10px] text-slate-500"
                                title="后置长度 S：与下一块重叠的窗口"
                            >
                                后置 S
                            </span>
                            <input
                                value={suffix}
                                onChange={(ev) => {
                                    setSuffix(ev.target.value);
                                    syncExecution(horizon, prefix, ev.target.value);
                                }}
                                disabled={!editable}
                                className={`${inputCls} min-w-0 flex-1`}
                            />
                        </label>
                        <label className="flex min-w-0 items-center gap-2">
                            <span
                                className="w-[5.5rem] shrink-0 truncate text-[10px] text-slate-500"
                                title="过渡策略：重叠段的动作聚合函数"
                            >
                                过渡策略
                            </span>
                            <select
                                value={aggregate}
                                onChange={(ev) => setAggregate(ev.target.value)}
                                disabled={!editable}
                                className={`${inputCls} min-w-0 flex-1`}
                            >
                                {AGGREGATE_FNS.map((fn) => (
                                    <option key={fn} value={fn}>
                                        {fn}
                                    </option>
                                ))}
                            </select>
                        </label>
                    </div>
                    {missing.length > 0 && (
                        <p className="mt-1 text-[11px] text-amber-300">
                            后端未上报字段：{missing.join(" / ")}（Edge 进程可能是旧代码：重启后刷新）。
                        </p>
                    )}
                    {issue !== null ? (
                        <p className="mt-1 text-[11px] text-rose-300">{issue}</p>
                    ) : (
                        <p className="mt-1 text-[11px] text-emerald-300">
                            P + E + S = {h} = H ✓　执行段 E 大于前置段 P ✓
                        </p>
                    )}
                    <Button
                        variant="default"
                        disabled={!editable || !valid}
                        title={
                            valid
                                ? "POST /v1/infers/rtc（infer rtc set）：写入 policy.rtc 并应用到运行中的 RTCManager"
                                : issue ?? ""
                        }
                        className="mt-2 !px-2 !py-1 text-[11px]"
                        onClick={() => void apply()}
                    >
                        应用 RTC 参数
                    </Button>
                </>
            )}

            <div className="mt-2 border-t border-slate-800 pt-1.5">
                {scaledNote && <p className="mb-1 text-[11px] text-amber-300">{scaledNote}</p>}
                <KvGrid>
                    <KeyValue
                        k="当前生效 H/P/E/S"
                        v={
                            rtc
                                ? `${rtcField(rtc.params.action_horizon) || "?"} / ${
                                      rtcField(rtc.params.prefix_len) || "?"
                                  } / ${rtcField(rtc.params.execution_horizon) || "auto"} / ${
                                      rtcField(rtc.params.suffix_len) || "?"
                                  }`
                                : "—"
                        }
                    />
                    <KeyValue
                        k="实际生效 P/E/S"
                        v={
                            rtc?.last_chunk
                                ? `${rtc.last_chunk.lens.prefix}/${rtc.last_chunk.lens.execution}/${rtc.last_chunk.lens.suffix}（${rtc.last_chunk.height} 步）`
                                : "—"
                        }
                    />
                    <KeyValue
                        k="过渡策略 / 重叠步数"
                        v={rtc ? `${rtc.params.aggregate_fn} / ${rtc.last_chunk?.overlap_steps ?? 0}` : "—"}
                    />
                </KvGrid>
            </div>
        </>
    );

    if (standalone) {
        return <Card title="RTC">{content}</Card>;
    }
    return <div className="mt-2 rounded border border-slate-800 bg-slate-950/40 p-2">{content}</div>;
}
