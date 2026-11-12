import { useState, type ReactNode } from "react";

import type { EdgeApi } from "../api";
import { useOps } from "../hooks/useOps";
import { usePolicyConfig } from "../hooks/usePolicyConfig";
import { errMsg, type CapturesStatus, type InferRolloutResponse, type InferStatus, type PolicyRef } from "../types";
import { InferOps } from "./InferOps";
import { RtcCard } from "./RtcCard";
import { PolicyConfigForm } from "./PolicyConfigForm";
import { RecordingControl } from "./RecordingControl";
import { Badge, Button, Card, ErrorText, Segmented } from "./ui";

type SessionKind = "capture" | "infer";

/**
 * **会话台**：采集与推理共用一张卡（同一时刻只可能有一个会话）。
 *
 * 结构按「进 / 用 / 退」三段拆开，避免把两套流程铺成两排按钮：
 *
 * 1. **未进入** —— 选会话类型 → （推理额外要策略配置）→ 一个主按钮「进入」；
 *    采集的「预检」是只读辅助，放同一行。
 * 2. **已进入** —— **统一录制控件**（采集 / 推理同一对命令，见 `RecordingControl`）
 *    + 该会话特有的操作（采集：元信息；推理：预热 / 单步 / 持续）+ 退出。
 * 3. **诊断** —— 推理的策略配置（只读）、RTC 参数、服务端 metadata：默认收起。
 *
 * 急停**不在这里**：它是全局安全动作（TopBar 按钮 + `Esc` 快捷键，最高优先旁路），
 * 与「本会话在做什么」无关，不重复占位。
 */
export function SessionDeck({
    api,
    hasLease,
    nodeState,
    sessionType,
    captures,
    infers,
    policies,
    onLog,
    fixedKind,
    onRollout,
    extra,
}: {
    api: EdgeApi | null;
    hasLease: boolean;
    nodeState: string | null;
    sessionType: string | null;
    captures: CapturesStatus | null;
    infers: InferStatus | null;
    policies: PolicyRef[];
    onLog: (msg: string, ok: boolean) => void;
    /** 固定会话类型（数采页 = capture / 推理页 = infer）：不再显示类型切换 */
    fixedKind?: SessionKind;
    /** 单步 / 持续推理回执（推理页据此把动作块喂给 3D 视口画期望轨迹） */
    onRollout?: (resp: InferRolloutResponse) => void;
    /** 追加在卡片内的页面专属内容（如采集页的采集状态：同一张卡里看状态 + 操作） */
    extra?: ReactNode;
}) {
    const [picked, setPicked] = useState<SessionKind>("capture");
    const kind = fixedKind ?? picked;
    const { busy, error, lastResult, run } = useOps(onLog);
    const cfg = usePolicyConfig({ infers, policies, sessionType, nodeState });

    const connected = api !== null;
    const inSession = nodeState === "active" && (sessionType === "capture" || sessionType === "infer");
    // 页面固定了会话类型（数采页 = capture / 推理页 = infer）时**以页面为准**：别的类型会话在跑
    // 也不能把本页卡片顶掉（否则推理页会被渲染成采集卡片，策略选择 / 配置表单整个消失）。只有
    // 通用会话卡（未固定类型）才跟随实际会话类型，并在会话进行中锁住类型切换。
    const shown: SessionKind = fixedKind ?? (inSession ? (sessionType as SessionKind) : kind);
    const isInfer = shown === "infer";
    /** 实际会话正是本卡片的类型：决定渲染「进入前」还是「会话内」分支（另一类型会话在跑时本页只
     *  展示自己的「进入前」配置 + 置灰的进入按钮，会话操作留给对应页面）。 */
    const inShownSession = inSession && sessionType === shown;
    const otherSession = inSession && sessionType !== shown;
    const sessionState = (isInfer ? infers?.state : captures?.state) ?? null;

    // 统一录制位：机器人进程权威，采集与推理状态里同源（`capture_status.running`）
    const liveCapture = (isInfer ? infers?.capture_status : captures?.capture_status) ?? null;
    const recording = liveCapture?.running ?? false;

    // 「进入」的共同门控：节点 READY + 无会话 + 持租约 + 已连接
    const enterBlocked = !connected
        ? "未连接 Edge"
        : !hasLease
        ? "需租约"
        : nodeState !== "ready"
        ? `节点未就绪（${nodeState ?? "—"}）`
        : sessionType !== null
        ? `已有会话进行中（${sessionType === "infer" ? "推理" : "采集"}，先退出再进入）`
        : isInfer && cfg.selectedPolicy === ""
        ? "未选择策略"
        : isInfer && !cfg.endpointSet
        ? `推理端点未配置（${cfg.endpointMissing.join("、")}）`
        : isInfer && !cfg.configReady
        ? `缺少必填项（${cfg.configMissing.join("、")}）`
        : null;

    /** 进入采集会话（POST /v1/captures）：空 body，单 adapter 包无选择项。 */
    const enterCapture = () => run("进入采集会话", () => api!.enter());

    /**
     * 进入推理会话 + **启动异步预热**（`POST /v1/infers` → `infer connect`）。
     *
     * 预热（连接策略服务端 + prepare + 取一块丢弃，**不下发动作**）可能几十秒～几分钟，
     * 后端立即回执，进度由 1s 轮询的状态（`warming` / `warmed_up` / `warmup_error`）驱动；
     * 预热期间急停 / 退出仍可用（后端把预热放在工作线程）。
     */
    const enterInfer = () =>
        run("进入并预热", async () => {
            const entered = await api!.enterInfer(cfg.selectedPolicy, cfg.configPayload());
            try {
                const connect = await api!.inferConnect();
                onLog(connect.warming ? "已启动预热（加载模型，看「预热中」徽标）" : "预热已就绪", true);
                return { entered, connect };
            } catch (e) {
                throw new Error(`${errMsg(e)}（会话已进入，可用「重试预热」再试）`);
            }
        });

    const exitSession = () => run("退出会话", () => (isInfer ? api!.exitInfer() : api!.exit()));

    const sessionCard = (
        <Card
            title={fixedKind ? (isInfer ? "Infer" : "Collect") : "Session"}
            actions={
                <>
                    {/* 卡内只留**本页看不到的状态**：通用会话卡的会话类型、推理的策略名。
                        会话状态 / 录制位分别由页面状态条与录制按钮承担，这里不重复。 */}
                    {fixedKind === undefined && inShownSession && (
                        <Badge tone="green">
                            {isInfer ? "推理" : "采集"} · {sessionState ?? "active"}
                        </Badge>
                    )}
                    {isInfer && inShownSession && <Badge tone="blue">{infers?.policy ?? "—"}</Badge>}
                </>
            }
        >
            {!inShownSession ? (
                <div className="space-y-2">
                    {otherSession && (
                        <p className="rounded border border-amber-700/60 bg-amber-950/30 px-2 py-1.5 text-[11px] text-amber-300">
                            当前正在运行{sessionType === "infer" ? "推理" : "采集"}会话；请先到对应页面退出，再进入
                            {isInfer ? "推理" : "采集"}。
                        </p>
                    )}
                    {fixedKind === undefined && (
                        <Segmented<SessionKind>
                            value={shown}
                            onChange={setPicked}
                            options={[
                                { value: "capture", label: "采集 Capture", title: "session run capture" },
                                { value: "infer", label: "推理 Infer", title: "session run infer" },
                            ]}
                        />
                    )}

                    {isInfer && (
                        <PolicyConfigForm
                            policies={policies}
                            connected={connected}
                            busy={busy}
                            locked={false}
                            framed={false}
                            cfg={cfg}
                        />
                    )}

                    <div className="flex flex-wrap items-center gap-2">
                        <Button
                            variant="primary"
                            disabled={busy || enterBlocked !== null}
                            title={
                                enterBlocked
                                    ? `不可用：${enterBlocked}`
                                    : isInfer
                                    ? "session run infer → POST /v1/infers（随后自动预热）"
                                    : "session run capture → POST /v1/captures"
                            }
                            onClick={() => void (isInfer ? enterInfer() : enterCapture())}
                        >
                            {isInfer ? "进入推理会话" : "进入采集会话"}
                        </Button>
                        {!isInfer && (
                            <Button
                                size="xs"
                                variant="ghost"
                                disabled={!connected || busy}
                                title="只读预检（GET /v1/captures/precheck，免租约）：节点 / 会话 / 机器人就绪 + 磁盘 + 可否签发租约"
                                onClick={() => void run("预检 Precheck", () => api!.capturesPrecheck())}
                            >
                                预检
                            </Button>
                        )}
                        {enterBlocked && <span className="text-[10px] text-amber-300">{enterBlocked}</span>}
                    </div>
                </div>
            ) : (
                <div className="space-y-2">
                    {isInfer && (
                        <PolicyConfigForm
                            policies={policies}
                            connected={connected}
                            busy={busy}
                            locked
                            framed={false}
                            cfg={cfg}
                        />
                    )}

                    <RecordingControl
                        api={api}
                        hasLease={hasLease}
                        connected={connected}
                        sessionType={sessionType}
                        inSession={inShownSession}
                        recording={recording}
                        prompt={cfg.promptValue}
                        serverPrompt={infers?.prompt ?? null}
                        requiresPrompt={isInfer && cfg.requiresPrompt}
                        captureMeta={liveCapture?.meta ?? {}}
                        busy={busy}
                        run={run}
                    />

                    {isInfer ? (
                        <InferOps
                            api={api}
                            hasLease={hasLease}
                            inInfer={inShownSession}
                            busy={busy}
                            run={run}
                            infers={infers}
                            onRollout={onRollout}
                        />
                    ) : null}

                    <div className="flex flex-wrap items-center gap-2 border-t border-slate-800 pt-2">
                        <Button
                            variant="danger"
                            disabled={busy || !hasLease}
                            title={
                                !hasLease
                                    ? "只有持有当前租约的页面才能退出会话"
                                    : `session quit → ${
                                          isInfer ? "DELETE /v1/infers?lease_id=" : "DELETE /v1/captures?lease_id="
                                      }`
                            }
                            onClick={() => void exitSession()}
                        >
                            退出会话
                        </Button>
                    </div>

                    {extra}

                    {lastResult && (
                        <pre className="max-h-20 overflow-auto rounded border border-slate-800 bg-slate-950/60 p-2 font-mono text-[10px] text-emerald-300">
                            {lastResult}
                        </pre>
                    )}
                    <ErrorText message={error} />
                </div>
            )}
        </Card>
    );

    if (!isInfer || fixedKind === undefined || (!inShownSession && cfg.selectedPolicy === "")) return sessionCard;

    return (
        <>
            {sessionCard}
            <RtcCard
                api={api}
                hasLease={hasLease}
                inInfer={inShownSession}
                busy={busy}
                rtc={infers?.rtc ?? null}
                run={run}
                standalone
            />
        </>
    );
}
