import type { EdgeApi } from "../api";
import { missingCaptureMeta } from "../captureMeta";
import { requireOk } from "../types";
import { Button } from "./ui";

/**
 * 录制控件（采集 / 推理共用）：未录制时显示“开始”，录制中显示“停止”与“取消”。
 *
 * 两侧落到的是**同一组命令** `capture episode start/end/cancel`（机器人进程不区分会话模式），
 * 录制位也同源（`capture_status.running`）。差异只在入口与前置动作：
 *
 * | 会话    | 入口                              | 前置动作                                                     |
 * | ------- | --------------------------------- | ------------------------------------------------------------ |
 * | capture | `POST /v1/commands`（capability） | 元信息由 Meta 卡「同步」提交                                  |
 * | infer   | `POST /v1/infers/episode/start`   | `capture sync`（`operator=policy`，语言条件策略带 task_name） |
 */
export function RecordingControl({
    api,
    hasLease,
    connected,
    sessionType,
    inSession,
    recording,
    prompt,
    serverPrompt,
    requiresPrompt,
    captureMeta,
    busy,
    run,
}: {
    api: EdgeApi | null;
    hasLease: boolean;
    connected: boolean;
    /** 当前会话类型（capture / infer；null = 未进入） */
    sessionType: string | null;
    /** 是否处于会话（ACTIVE + 已知会话类型） */
    inSession: boolean;
    /** 录制位（**机器人进程权威**：`capture_status.running`） */
    recording: boolean;
    /** 策略配置里 prompt 的当前输入值（推理侧作 episode 的 task_name） */
    prompt: string;
    /** 后端当前生效的 prompt（与输入不一致时先写入再开录） */
    serverPrompt: string | null;
    requiresPrompt: boolean;
    /** 机器人进程当前持有的采集 meta；人工采集必须已有 operator / task_name。 */
    captureMeta: Record<string, unknown>;
    busy: boolean;
    run: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
    const isInfer = sessionType === "infer";
    const taskName = prompt.trim();
    const missingMeta = isInfer ? [] : missingCaptureMeta(captureMeta);

    const blocked = !connected
        ? "未连接"
        : !hasLease
        ? "需租约"
        : !inSession
        ? "需进入会话"
        : isInfer && requiresPrompt && taskName === ""
        ? "prompt 为空"
        : null;
    // 元信息只门禁「开始」；录制已经在进行时，即使状态暂时没读到 meta，也必须允许停止 / 取消。
    const startBlocked = blocked ?? (missingMeta.length > 0 ? `请先选择并同步${missingMeta.join("、")}` : null);

    const start = () => {
        if (!api) return;
        return run("开始录制", async () => {
            if (!isInfer) return requireOk(api.command("capture/episode/start"));
            const meta: Record<string, unknown> = { operator: "policy" };
            if (requiresPrompt) {
                if (taskName !== (serverPrompt ?? "")) await api.setInferConfig({ prompt: taskName });
                meta.task_name = taskName;
            }
            await api.inferSyncMeta(meta);
            return api.inferRecordStart();
        });
    };

    const stop = () => {
        if (!api) return;
        return run("停止录制", () => (isInfer ? api.inferRecordEnd() : requireOk(api.command("capture/episode/end"))));
    };

    const cancel = () => {
        if (!api) return;
        if (!window.confirm("取消录制会删除本轮已经录制的数据，且无法恢复。确定继续吗？")) return;
        return run("取消录制", () =>
            isInfer ? api.inferRecordCancel() : requireOk(api.command("capture/episode/cancel"))
        );
    };

    return (
        <div className="flex flex-wrap items-center gap-2 border-t border-slate-800 pt-2">
            {recording ? (
                <>
                    <Button
                        disabled={busy || blocked !== null}
                        title={blocked ?? "capture episode end（保存本轮）"}
                        onClick={() => void stop()}
                    >
                        ■ 停止录制
                    </Button>
                    <Button
                        variant="danger"
                        disabled={busy || blocked !== null}
                        title={blocked ?? "capture episode cancel（删除本轮）"}
                        onClick={() => void cancel()}
                    >
                        × 取消录制
                    </Button>
                </>
            ) : (
                <Button
                    variant="primary"
                    disabled={busy || startBlocked !== null}
                    title={startBlocked ?? "capture episode start"}
                    onClick={() => void start()}
                >
                    ● 开始录制
                </Button>
            )}
            {(recording ? blocked : startBlocked) && (
                <span className="text-[10px] text-amber-400">{recording ? blocked : startBlocked}</span>
            )}
        </div>
    );
}
