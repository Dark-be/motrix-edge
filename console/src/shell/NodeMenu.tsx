import { Button } from "../components/ui";
import { useEdge } from "../edge";
import { Chip, type ChipTone } from "./controls";

const stateTone: Record<string, ChipTone> = {
    ready: "green",
    active: "blue",
    idle: "slate",
    error: "red",
};

/** 顶栏节点状态 + 独立复位按钮；机器人回 home 在各页面操作区执行。 */
export function NodeMenu() {
    const { nodeState, hasLease, resetNode } = useEdge();
    const tone = stateTone[(nodeState ?? "").toLowerCase()] ?? "slate";

    return (
        <div className="flex items-center gap-1.5">
            <Chip tone={tone} label="state" value={nodeState ?? "—"} />
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
        </div>
    );
}
