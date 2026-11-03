import { Card } from "../components/ui";
import { PageLayout } from "../layout/PageLayout";
import { StatusStrip } from "../shell/StatusStrip";

/**
 * 05 REPLAY / 06 RESULT —— 占位页。
 *
 * Edge 的 HTTP 控制面目前没有回放 / 结果接口（回放要看本地 episode 文件，结果要等评测流程），
 * 所以这两页只登记「将来放什么、缺什么接口」，避免假装有功能。
 */
export function PlaceholderPage({ page, no, todo }: { page: string; no: string; todo: string[] }) {
    return (
        <>
            <StatusStrip mode={page} tone="slate" />
            <PageLayout
                left={
                    <Card title={`${no} ${page}`} subtitle="占位">
                        <p className="text-[11px] text-muted">
                            这一页先占位：标签结构已就位（数采 / 推理各自独立），后续按需求把对应流程填进来，
                            不必改动其它页面。
                        </p>
                    </Card>
                }
                center={
                    <Card title="待接入" subtitle="needs edge API">
                        <ul className="list-disc space-y-1 pl-5 text-[11px] text-muted">
                            {todo.map((item) => (
                                <li key={item}>{item}</li>
                            ))}
                        </ul>
                    </Card>
                }
                right={
                    <Card title="说明" subtitle="why empty">
                        <p className="text-[11px] text-muted">
                            前端只消费 Edge 已提供的 `/v1/*` 契约；没有后端接口时不编造数据（宁缺勿假）。
                        </p>
                    </Card>
                }
            />
        </>
    );
}

export function ReplayPage() {
    return (
        <PlaceholderPage
            page="REPLAY"
            no="05"
            todo={[
                "episode 回放接口（读取本地 mcap / JSON 并按帧回放观测与动作）",
                "回放时间轴：与 3D 视口、相机轨对齐（当前相机只有实时 WebRTC 一路）",
                "回放期间的只读保护（禁止下发，避免与真机操作混淆）",
            ]}
        />
    );
}

export function ResultPage() {
    return (
        <PlaceholderPage
            page="RESULT"
            no="06"
            todo={[
                "评测 / 任务结果接口（成功率、episode 列表、指标曲线）",
                "结果与 episode / rollout 录制的关联（需要远端评测服务或本地汇总）",
                "导出（CSV / 图表）与按任务筛选",
            ]}
        />
    );
}
