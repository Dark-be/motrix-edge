import { useState } from "react";

import { Button } from "./ui";

export interface LogEntry {
    ts: number;
    msg: string;
    ok: boolean;
}

/** 操作日志抽屉：左下角常驻按钮 + 抽屉列表。
 *
 * 放抽屉而不是页面底部卡片的原因：操作结果要「随时能翻」，但不该永远占着版面
 * （结果的第一时间反馈由右下角 toast 承担）。
 */
export function LogPanel({ entries, onClear }: { entries: LogEntry[]; onClear: () => void }) {
    const [open, setOpen] = useState(false);
    const failed = entries.filter((e) => !e.ok).length;
    const toggle = (
        <button
            type="button"
            onClick={() => setOpen(!open)}
            title={open ? "收起操作日志" : "展开操作日志"}
            className="rounded-md border border-slate-700 bg-slate-900/90 px-2.5 py-1 text-xs text-slate-300 shadow-lg hover:border-slate-500 hover:text-slate-100"
        >
            日志 {entries.length}
            {failed > 0 && <span className="ml-1 text-rose-300">· 失败 {failed}</span>}
            <span className="ml-1 text-slate-500">{open ? "▾" : "▴"}</span>
        </button>
    );

    if (!open) {
        return <div className="fixed bottom-4 left-4 z-40">{toggle}</div>;
    }
    return (
        <div className="fixed bottom-4 left-4 z-40 flex max-h-[60vh] w-[30rem] max-w-[calc(100vw-2rem)] flex-col rounded-md border border-slate-700 bg-slate-900/95 shadow-xl backdrop-blur">
            <div className="flex items-center justify-between gap-2 border-b border-slate-800 px-3 py-1.5">
                <span className="text-xs font-semibold text-slate-200">
                    操作日志<span className="ml-1 font-normal text-slate-500">（结果记录，调试用）</span>
                </span>
                <div className="flex items-center gap-2">
                    <Button
                        variant="ghost"
                        className="!px-2 !py-0.5 text-[11px]"
                        disabled={entries.length === 0}
                        onClick={onClear}
                    >
                        清空
                    </Button>
                    <Button variant="ghost" className="!px-2 !py-0.5 text-[11px]" onClick={() => setOpen(false)}>
                        收起
                    </Button>
                </div>
            </div>
            {entries.length === 0 ? (
                <p className="px-3 py-3 text-xs text-slate-500">暂无操作记录。</p>
            ) : (
                <ul className="space-y-0.5 overflow-auto px-3 py-2 font-mono text-[11px]">
                    {entries.map((e, i) => (
                        <li key={`${e.ts}-${i}`} className="flex gap-2">
                            <span className="shrink-0 text-slate-600">
                                {new Date(e.ts).toLocaleTimeString("zh-CN", { hour12: false })}
                            </span>
                            <span className={`break-all ${e.ok ? "text-emerald-300" : "text-rose-300"}`}>{e.msg}</span>
                        </li>
                    ))}
                </ul>
            )}
        </div>
    );
}
