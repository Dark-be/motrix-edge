export type PageId = "manual" | "collect" | "infer" | "debug";

/** 标签页（编号 + 名称）：每个标签是**独立页面**——数采 / 推理各自成页，后续加页只往这里加一条。 */
export const PAGES: { id: PageId; no: string; label: string; title: string }[] = [
    { id: "manual", no: "01", label: "MANUAL", title: "手动：按动作空间拖滑块 → SEND 下发（点发送才生效）" },
    { id: "collect", no: "02", label: "COLLECT", title: "数采：进入采集会话 → 轮次开始 / 结束 + 元信息同步" },
    { id: "infer", no: "03", label: "INFER", title: "推理：策略配置 → 连接 → 单步 / 持续推理 → 录制" },
    { id: "debug", no: "04", label: "DEBUG", title: "调试：适配器能力 / 观测数值 / 原语 / 深度 / 上传 / 日志" },
];

/** 顶部标签栏：编号 + 名称，当前页高亮（与页面内容同为浅 / 深色一套令牌）。 */
export function TabBar({ active, onChange }: { active: PageId; onChange: (id: PageId) => void }) {
    return (
        <nav className="flex flex-wrap items-stretch gap-1 border-b border-line bg-panel px-3">
            {PAGES.map((page) => {
                const isActive = page.id === active;
                return (
                    <button
                        key={page.id}
                        type="button"
                        onClick={() => onChange(page.id)}
                        title={page.title}
                        aria-current={isActive ? "page" : undefined}
                        className={`-mb-px flex items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-1.5 text-[11px] font-medium tracking-wide transition-colors ${
                            isActive
                                ? "border-accent text-accent"
                                : "border-transparent text-muted hover:border-line2 hover:text-ink"
                        }`}
                    >
                        <span className="tnum font-mono text-[10px] opacity-70">{page.no}</span>
                        {page.label}
                    </button>
                );
            })}
        </nav>
    );
}
