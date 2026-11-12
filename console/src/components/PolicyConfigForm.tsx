import type { usePolicyConfig } from "../hooks/usePolicyConfig";
import type { PolicyConfigItemValue, PolicyRef } from "../types";
import { Badge, BlockTitle, plain } from "./ui";

/**
 * 策略配置表单（**按所选策略的 schema 动态生成**，前端不硬编码任何键名）。
 *
 * - 未进入会话：随「进入并预热」的 body 一次性下发（`POST /v1/infers` 的 `config`）；
 * - 会话内：`locked` = 只读（策略已按这份配置建连 / 加载模型，改动对本次会话不生效）；
 *   确需热改的 `runtime=True` 键（如 prompt）走 `POST /v1/infers/config`。
 */
export function PolicyConfigForm({
    policies,
    connected,
    busy,
    locked,
    framed = true,
    cfg,
}: {
    policies: PolicyRef[];
    connected: boolean;
    busy: boolean;
    /** 会话内只读（置灰；要改先退出会话） */
    locked: boolean;
    /** false 时作为顶层 Card 的正文使用，避免出现卡片套卡片。 */
    framed?: boolean;
    cfg: ReturnType<typeof usePolicyConfig>;
}) {
    const { activePolicyType, configItems, configValue, setField, configMissing, configReady, endpointMissing } = cfg;
    // 选策略 / 填配置只改本地草稿（不发请求）→ 不要求租约，只在「进入并预热」时校验；
    // 会话内整张表单只读（`locked`）。
    const disabled = !connected || busy || locked;

    return (
        <div className={framed ? "rounded border border-slate-800 bg-slate-950/40 p-2" : ""}>
            <div className="mb-2 flex flex-wrap items-end gap-2">
                <label className="flex min-w-0 flex-1 flex-col gap-0.5" htmlFor="infer-policy">
                    <span className="text-[10px] uppercase tracking-wide text-slate-500">
                        推理策略（进入会话前必选）
                    </span>
                    <select
                        id="infer-policy"
                        value={activePolicyType}
                        onChange={(e) => cfg.setSelectedPolicy(e.target.value)}
                        disabled={disabled}
                        className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 disabled:opacity-50"
                    >
                        <option value="">请选择策略</option>
                        {policies.map((policy) => (
                            <option key={policy.type} value={policy.type}>
                                {policy.type} — {policy.class}
                            </option>
                        ))}
                    </select>
                </label>
                {policies.length === 0 && (
                    <span className="text-[11px] text-amber-300">未发现可用策略，无法进入推理会话。</span>
                )}
            </div>

            <BlockTitle
                actions={
                    <Badge tone={configReady ? "green" : "amber"}>
                        {configItems.length === 0
                            ? "无额外配置项"
                            : configReady
                            ? "已配置"
                            : `缺 ${configMissing.join("、")}`}
                    </Badge>
                }
            >
                策略配置
            </BlockTitle>

            {configItems.length === 0 ? (
                <p className="text-[11px] text-slate-500">{activePolicyType === "" ? "选择策略" : "无额外配置项"}</p>
            ) : (
                <div className="grid grid-cols-2 gap-2">
                    {configItems.map((item) => (
                        <ConfigField
                            key={item.key}
                            item={item}
                            value={configValue(item)}
                            disabled={disabled}
                            onChange={(value) => setField(item.key, value)}
                        />
                    ))}
                </div>
            )}

            {!cfg.endpointSet && (
                <p className="mt-1 text-[11px] text-rose-300">推理端点未配置（{endpointMissing.join("、")}）</p>
            )}
            {!configReady && <p className="mt-1 text-[11px] text-rose-300">缺少必填（{configMissing.join("、")}）</p>}
        </div>
    );
}

/** 单个配置项：`options` → 固定下拉；否则按 `type` / `multiline` 渲染。 */
function ConfigField({
    item,
    value,
    disabled,
    onChange,
}: {
    item: PolicyConfigItemValue;
    value: string;
    disabled: boolean;
    onChange: (value: string) => void;
}) {
    const cls =
        "rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 disabled:opacity-50";
    return (
        <label className={`flex min-w-0 flex-col gap-0.5${item.multiline ? " col-span-2" : ""}`}>
            <span className="truncate text-[10px] uppercase tracking-wide text-slate-500" title={item.label}>
                {item.label}
                {item.required && <span className="text-rose-300"> *</span>}
            </span>
            {item.options && item.options.length > 0 ? (
                <select value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled} className={cls}>
                    {item.options.map((option) => (
                        <option key={String(option)} value={String(option)}>
                            {String(option)}
                        </option>
                    ))}
                </select>
            ) : item.type === "bool" ? (
                <select
                    value={value || "false"}
                    onChange={(e) => onChange(e.target.value)}
                    disabled={disabled}
                    className={cls}
                >
                    {["true", "false"].map((option) => (
                        <option key={option} value={option}>
                            {option}
                        </option>
                    ))}
                </select>
            ) : item.multiline ? (
                /* 长文本项（schema 标 multiline，如 prompt / system_prompt）：占满一行且可拖拽调整高度 */
                <textarea
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    placeholder={item.placeholder ?? item.key}
                    rows={item.rows ?? 4}
                    spellCheck
                    disabled={disabled}
                    className={`w-full resize-y leading-relaxed ${cls}`}
                />
            ) : (
                <input
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    placeholder={
                        item.secret && item.configured && value === ""
                            ? "已设置（输入新值可覆盖）"
                            : item.placeholder ?? item.key
                    }
                    type={item.secret ? "password" : item.type === "int" || item.type === "float" ? "number" : "text"}
                    step={item.type === "float" ? "any" : undefined}
                    autoComplete={item.secret ? "off" : undefined}
                    spellCheck={item.secret ? false : undefined}
                    disabled={disabled}
                    title={
                        item.secret
                            ? "仅存 edge 内存态：不落盘、不回显，刷新页面后需重填"
                            : plain(item.help) || undefined
                    }
                    className={cls}
                />
            )}
            {item.secret && (
                <span className="text-[10px] text-amber-300/80">
                    {item.configured && value === ""
                        ? "内存态已设置（刷新后此框会清空）"
                        : "仅本次运行有效：不落盘、不回显"}
                </span>
            )}
        </label>
    );
}
