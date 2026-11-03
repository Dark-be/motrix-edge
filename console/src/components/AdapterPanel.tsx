import { useEffect, useState } from "react";

import type { EdgeApi } from "../api";
import { errMsg, requireOk, type AdapterEffectiveConfig, type BoundAdapter } from "../types";
import { Badge, Button, brief, Card, ErrorText } from "./ui";

export function AdapterPanel({
    api,
    adapter,
    hasLease,
    onLog,
}: {
    api: EdgeApi | null;
    adapter: BoundAdapter | null;
    hasLease: boolean;
    onLog: (msg: string, ok: boolean) => void;
}) {
    // 机器人命令控制区（reset / teleop / takeover / estop）；动作下发统一放到 Manual 滑块页。
    const [busy, setBusy] = useState(false);
    const [cmdError, setCmdError] = useState<string | null>(null);
    const [configError, setConfigError] = useState<string | null>(null);
    const [lastResult, setLastResult] = useState<string | null>(null);
    // 能力勾选状态（来自 GET /v1/adapters/current 的 enabled 字典；勾选 → 同步到 Edge）
    const [armChecks, setArmChecks] = useState<Record<string, boolean>>({});
    const [camChecks, setCamChecks] = useState<Record<string, boolean>>({});
    // 当前 adapter 实际生效能力（GET /v1/adapters/current；action_dim 等只读展示）
    const [effective, setEffective] = useState<AdapterEffectiveConfig | null>(null);
    // 收起 / 展开（暂时不用时可收起，头部保留「可发命令」徽章）
    const [collapsed, setCollapsed] = useState(false);

    const loadEffective = () => {
        if (!api) return;
        api.adapterCurrent()
            .then((e) => {
                setEffective(e);
                setArmChecks(e.enabled?.arms ?? {});
                setCamChecks(e.enabled?.cameras ?? {});
            })
            .catch(() => {
                setEffective(null);
                setArmChecks({});
                setCamChecks({});
            });
    };
    useEffect(() => {
        loadEffective();
    }, [api]);

    const toggleArm = (name: string) => setArmChecks((p) => ({ ...p, [name]: !p[name] }));
    const toggleCam = (name: string) => setCamChecks((p) => ({ ...p, [name]: !p[name] }));

    // 勾选结果 → 同步到 Edge（enabled_arms / enabled_cameras → POST /v1/adapters/config）
    const applyCfg = () => {
        if (!api) return;
        setBusy(true);
        setConfigError(null);
        api.setAdapterConfig({
            enabled_arms: Object.keys(armChecks).filter((k) => armChecks[k]),
            enabled_cameras: Object.keys(camChecks).filter((k) => camChecks[k]),
        })
            .then(() => {
                onLog("同步 Adapter 配置 成功", true);
                loadEffective();
            })
            .catch((e) => {
                const msg = errMsg(e);
                setConfigError(msg);
                onLog(`同步 Adapter 配置 失败: ${msg}`, false);
            })
            .finally(() => setBusy(false));
    };

    const runCmd = async (label: string, fn: () => Promise<unknown>) => {
        if (!api) return;
        setBusy(true);
        setCmdError(null);
        try {
            const res = await fn();
            const summary = brief(res);
            setLastResult(`${label} → ${JSON.stringify(res)}`);
            onLog(`${label} 成功: ${summary}`, true);
        } catch (e) {
            const msg = errMsg(e);
            setCmdError(msg);
            setLastResult(`${label} 失败`);
            onLog(`${label} 失败: ${msg}`, false);
        } finally {
            setBusy(false);
        }
    };

    const connected = api !== null;
    const cmdEnabled = connected && hasLease && adapter !== null;
    // 遥操作 / 人工接管状态（Edge 上报；开启期间推理 rollout 会被进程拒绝）
    const teleop = adapter?.teleop ?? false;
    const teleopMode = adapter?.teleop_mode ?? null;
    const teleopLabel = !teleop
        ? "程控"
        : teleopMode === "delta"
        ? "人工接管（增量）"
        : `遥操作（${teleopMode ?? "absolute"}）`;

    const command = (label: string, capability: string, args?: Record<string, unknown>) =>
        void runCmd(label, () => requireOk(api!.command(capability, args)));

    return (
        <>
            <Card
                title="Command"
                collapsible
                collapsed={collapsed}
                onToggle={() => setCollapsed(!collapsed)}
                actions={
                    <>
                        <Badge tone={teleop ? "amber" : "slate"}>{teleopLabel}</Badge>
                        <Badge tone={cmdEnabled ? "green" : "amber"}>
                            {cmdEnabled ? "可发命令" : adapter === null ? "未绑定适配器" : "需激活租约"}
                        </Badge>
                    </>
                }
            >
                <div className="space-y-1.5">
                    <div className="grid grid-cols-2 gap-1.5">
                        <Button
                            disabled={!cmdEnabled || busy}
                            title="robot reset：回 home（非阻塞）"
                            onClick={() => command("复位", "robot/reset")}
                        >
                            复位
                        </Button>
                        <Button
                            disabled={!cmdEnabled || busy || (teleop && teleopMode === "delta")}
                            title="robot teleop true delta：以接管瞬间为锚点叠加主臂增量"
                            onClick={() => command("人工接管", "robot/teleop", { enabled: true, mode: "delta" })}
                        >
                            人工接管
                        </Button>
                        <Button
                            variant="primary"
                            disabled={!cmdEnabled || busy || (teleop && teleopMode !== "delta")}
                            title="robot teleop true"
                            onClick={() => command("遥操作 ON", "robot/teleop", { enabled: true })}
                        >
                            遥操作 ON
                        </Button>
                        <Button
                            disabled={!cmdEnabled || busy || !teleop}
                            title="robot teleop false"
                            onClick={() => command("遥操作 OFF", "robot/teleop", { enabled: false })}
                        >
                            遥操作 OFF
                        </Button>
                    </div>
                    <Button
                        variant="danger"
                        className="w-full"
                        disabled={!cmdEnabled || busy}
                        title="robot/estop（快捷键 Esc）"
                        onClick={() => command("急停", "robot/estop")}
                    >
                        ■ 急停
                    </Button>
                </div>

                {lastResult && (
                    <pre className="mt-2 max-h-20 overflow-auto rounded border border-slate-800 bg-slate-950/60 p-2 font-mono text-[10px] text-emerald-300">
                        {lastResult}
                    </pre>
                )}
                <ErrorText message={cmdError} />
            </Card>

            {/* 运行时 adapter 能力配置（勾选启用臂 / 相机；来自 GET /v1/adapters/current，同步 POST /v1/adapters/config） */}
            <Card
                title="Adapter"
                actions={
                    <>
                        {!effective && (
                            <span
                                className="rounded border border-amber-800/60 bg-amber-950/30 px-1.5 py-px text-[10px] text-amber-300"
                                title="能力布局（臂名 / 相机名）来自 adapter 类常量、discover 不传；未绑定适配器就没有机型信息，GET /v1/adapters/current 回 404（不猜默认）。等机器人进程上线、adapter 绑定后才可见勾选项。"
                            >
                                未绑定
                            </span>
                        )}
                        {effective && (
                            <span className="text-[10px] text-slate-500">
                                动作维度:{" "}
                                <span className="font-mono text-emerald-300">
                                    {Object.entries(effective.action_dims ?? {})
                                        .map(([s, d]) => `${s}=${d}`)
                                        .join(" ") || "—"}
                                </span>
                            </span>
                        )}
                        <Button
                            size="xs"
                            disabled={!connected || busy}
                            title="重新读取当前 adapter 实际生效配置（GET /v1/adapters/current）"
                            onClick={loadEffective}
                        >
                            刷新
                        </Button>
                    </>
                }
            >
                <div className="grid gap-2">
                    <fieldset className="rounded border border-slate-800 bg-slate-950/60 p-1.5">
                        <legend className="px-1 text-[10px] uppercase tracking-wide text-slate-500">机械臂 arms</legend>
                        {Object.keys(armChecks).length === 0 ? (
                            <div className="py-0.5 text-[11px] text-slate-600">—（未绑定适配器）</div>
                        ) : (
                            Object.entries(armChecks).map(([name, on]) => (
                                <label
                                    key={name}
                                    className="flex cursor-pointer items-center gap-1.5 py-0.5 text-[11px] text-slate-200"
                                >
                                    <input
                                        type="checkbox"
                                        checked={on}
                                        disabled={!connected || !hasLease || busy}
                                        onChange={() => toggleArm(name)}
                                        className="h-3.5 w-3.5 accent-sky-600 disabled:opacity-40"
                                    />
                                    <span className="font-mono">{name}</span>
                                    {on ? (
                                        <span className="text-emerald-300">on</span>
                                    ) : (
                                        <span className="text-slate-500">off</span>
                                    )}
                                </label>
                            ))
                        )}
                    </fieldset>
                    <fieldset className="rounded border border-slate-800 bg-slate-950/60 p-1.5">
                        <legend className="px-1 text-[10px] uppercase tracking-wide text-slate-500">
                            相机 cameras
                        </legend>
                        {Object.keys(camChecks).length === 0 ? (
                            <div className="py-0.5 text-[11px] text-slate-600">—（未绑定适配器）</div>
                        ) : (
                            Object.entries(camChecks).map(([name, on]) => (
                                <label
                                    key={name}
                                    className="flex cursor-pointer items-center gap-1.5 py-0.5 text-[11px] text-slate-200"
                                >
                                    <input
                                        type="checkbox"
                                        checked={on}
                                        disabled={!connected || !hasLease || busy}
                                        onChange={() => toggleCam(name)}
                                        className="h-3.5 w-3.5 accent-sky-600 disabled:opacity-40"
                                    />
                                    <span className="font-mono">{name}</span>
                                    {on ? (
                                        <span className="text-emerald-300">on</span>
                                    ) : (
                                        <span className="text-slate-500">off</span>
                                    )}
                                </label>
                            ))
                        )}
                    </fieldset>
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                    <Button
                        size="xs"
                        variant="primary"
                        disabled={!connected || !hasLease || busy || !effective}
                        title={
                            effective
                                ? "把勾选结果同步到 Edge（POST /v1/adapters/config，须持有租约）；未启用臂动作用 adapter 类常量 HOME[space] 填充"
                                : "未绑定适配器：无可勾选项（等机器人进程上线、adapter 绑定后再配置）"
                        }
                        onClick={applyCfg}
                    >
                        同步到 Edge
                    </Button>
                </div>
                <ErrorText message={configError} />
            </Card>
        </>
    );
}
