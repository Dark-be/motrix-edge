import { useEdge } from "../edge";
import { Button } from "../components/ui";
import { Dropdown } from "./Dropdown";
import { Chip, type ChipTone } from "./controls";

const stateTone: Record<string, ChipTone> = {
    ready: "green",
    active: "blue",
    idle: "slate",
    error: "red",
};

/**
 * 顶栏「节点」下拉：收起时只显示节点状态，展开看会话 / 适配器 / 磁盘 + 复位。
 *
 * 节点复位（`node reset`：ERROR → IDLE）与机器人复位（`robot reset`：回 home）都是低频动作，
 * 收进下拉即可，不必占主区。
 */
export function NodeMenu() {
    const { captures, adapter, nodeState, sessionType, sessionState, health, hasLease, resetNode, run, api } =
        useEdge();
    const tone = stateTone[(nodeState ?? "").toLowerCase()] ?? "slate";
    const teleop = adapter?.teleop ?? false;

    return (
        <Dropdown
            panelClass="w-[22rem]"
            title="节点状态与复位"
            label={<Chip tone={tone} label="state" value={nodeState ?? "—"} />}
        >
            <div className="space-y-2">
                <div className="space-y-1 rounded border border-line bg-panel2 p-2 text-[11px]">
                    <Row k="会话类型" v={sessionType ?? "无"} />
                    <Row k="会话状态" v={sessionState ?? "—"} />
                    <Row k="控制位" v={teleop ? `遥操作 / 接管（${adapter?.teleop_mode ?? "absolute"}）` : "程控"} />
                    <Row
                        k="适配器"
                        v={
                            adapter
                                ? `${adapter.name ?? "—"} · ${adapter.type ?? "—"}${
                                      adapter.running ? " · running" : ""
                                  }`
                                : "未绑定"
                        }
                    />
                    <Row
                        k="控制频率"
                        v={
                            adapter
                                ? `${adapter.control_hz ?? "—"} Hz 名义 / ${adapter.measured_hz ?? "—"} Hz 实测`
                                : "—"
                        }
                    />
                    <Row
                        k="磁盘剩余"
                        v={health?.disk?.free !== undefined ? `${(health.disk.free / 1e9).toFixed(1)} GB` : "—"}
                    />
                    <Row k="数据目录" v={captures?.capture_status?.data_dir ?? "—"} />
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                    <Button
                        size="xs"
                        variant="danger"
                        disabled={!(nodeState === "error" && hasLease)}
                        title={
                            nodeState !== "error"
                                ? "仅 ERROR 状态可节点复位（node reset）"
                                : !hasLease
                                ? "需先持有租约"
                                : "node reset：ERROR → IDLE"
                        }
                        onClick={resetNode}
                    >
                        节点复位
                    </Button>
                    <Button
                        size="xs"
                        disabled={!hasLease || nodeState === "idle"}
                        title={!hasLease ? "需先持有租约" : "robot reset：回 home"}
                        onClick={() => void run("robot reset", () => api!.command("robot/reset"))}
                    >
                        机器人复位
                    </Button>
                </div>
                <p className="text-[10px] text-faint">
                    节点复位只在 ERROR 状态可用；机器人复位等价 <code className="font-mono">robot reset</code>
                    （回 home，非阻塞）。
                </p>
            </div>
        </Dropdown>
    );
}

function Row({ k, v }: { k: string; v: string }) {
    return (
        <div className="flex items-baseline justify-between gap-2">
            <span className="shrink-0 text-[10px] uppercase tracking-wide text-faint">{k}</span>
            <span className="tnum min-w-0 truncate font-mono text-[11px] text-ink" title={v}>
                {v}
            </span>
        </div>
    );
}
