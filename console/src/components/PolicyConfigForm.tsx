import { useEffect, useRef, useState } from "react";

import { parsePolicyImport } from "../configImport";
import type { usePolicyConfig } from "../hooks/usePolicyConfig";
import { loadInferPresets, type InferPreset } from "../inferPresets";
import type { PolicyConfigItemValue, PolicyRef } from "../types";
import { Badge, Button } from "./ui";

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
    cacheScope,
}: {
    policies: PolicyRef[];
    connected: boolean;
    busy: boolean;
    /** 会话内只读（置灰；要改先退出会话） */
    locked: boolean;
    /** false 时作为顶层 Card 的正文使用，避免出现卡片套卡片。 */
    framed?: boolean;
    cfg: ReturnType<typeof usePolicyConfig>;
    /** 按 Edge 地址隔离最近一次已解析的完整策略 YAML。 */
    cacheScope: string;
}) {
    const { activePolicyType, configItems, configValue, configMissing, configReady, endpointMissing } = cfg;
    const [yamlText, setYamlText] = useState("");
    const [selectedPresetId, setSelectedPresetId] = useState("");
    const [parseError, setParseError] = useState<string | null>(null);
    const [presetCatalog, setPresetCatalog] = useState<InferPreset[] | null>(null);
    const [presetLoadError, setPresetLoadError] = useState<string | null>(null);
    const edited = useRef(false);
    const initialized = useRef(false);
    const availablePresets = (presetCatalog ?? []).filter((preset) =>
        policies.some((policy) => policy.type === preset.policyType)
    );
    const selectedPreset = availablePresets.find((preset) => preset.id === selectedPresetId) ?? null;
    const cacheKey = `edge-console:policy-config:${cacheScope}`;
    // 选策略 / 填配置只改本地草稿（不发请求）→ 不要求租约，只在「进入并预热」时校验；
    // 会话内整张表单只读（`locked`）。
    const disabled = !connected || busy || locked;
    const syncConfig = (source: string, persist = false) => {
        try {
            const parsed = parsePolicyImport(source, policies);
            cfg.replaceDraft(parsed.policyType, parsed.draft);
            if (persist) localStorage.setItem(cacheKey, source);
            setParseError(null);
            return true;
        } catch (error) {
            setParseError(error instanceof Error ? error.message : String(error));
            return false;
        }
    };
    const parseConfig = () => {
        syncConfig(yamlText, true);
    };
    const applyPreset = (preset: InferPreset) => {
        edited.current = false;
        setSelectedPresetId(preset.id);
        setYamlText(preset.source);
        syncConfig(preset.source, true);
    };

    useEffect(() => {
        let cancelled = false;
        loadInferPresets()
            .then((presets) => {
                if (!cancelled) setPresetCatalog(presets);
            })
            .catch((error) => {
                if (cancelled) return;
                setPresetLoadError(error instanceof Error ? error.message : String(error));
                setPresetCatalog([]);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    useEffect(() => {
        if (initialized.current || edited.current || availablePresets.length === 0 || locked) return;
        initialized.current = true;
        const cached = localStorage.getItem(cacheKey);
        if (cached && syncConfig(cached)) {
            setYamlText(cached);
            setSelectedPresetId(availablePresets.find((preset) => preset.source === cached)?.id ?? "");
            return;
        }
        const initial =
            availablePresets.find((preset) => preset.policyType === activePolicyType) ?? availablePresets[0];
        applyPreset(initial);
        // cfg is intentionally represented by stable setters plus the current policy list.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [policies, locked, activePolicyType, cacheKey, presetCatalog]);

    const configuredCount = configItems.filter((item) => configValue(item).trim() !== "").length;
    const rtc = (() => {
        const item = configItems.find((candidate) => candidate.key === "rtc");
        if (!item) return null;
        try {
            return JSON.parse(configValue(item)) as {
                enabled?: boolean;
                action_horizon?: number;
                prefix_len?: number;
                execution_horizon?: number | null;
                suffix_len?: number;
            };
        } catch {
            return null;
        }
    })();

    return (
        <div className={framed ? "rounded border border-slate-800 bg-slate-950/40 p-2" : ""}>
            <div className="flex flex-wrap items-center gap-2">
                <label className="flex min-w-[15rem] flex-1 items-center gap-2">
                    <span className="shrink-0 text-[10px] uppercase tracking-wide text-slate-500">策略配置</span>
                    <select
                        value={selectedPresetId}
                        disabled={disabled || availablePresets.length === 0}
                        onChange={(event) => {
                            const preset = availablePresets.find((candidate) => candidate.id === event.target.value);
                            if (preset) applyPreset(preset);
                        }}
                        className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-100 disabled:opacity-50"
                    >
                        {availablePresets.length === 0 && <option value="">无可用预设</option>}
                        {availablePresets.map((preset) => (
                            <option key={preset.id} value={preset.id}>
                                {preset.label}
                            </option>
                        ))}
                    </select>
                </label>
                <div className="flex min-w-0 items-center gap-1.5">
                    <Badge tone={configReady && cfg.endpointSet ? "green" : "amber"}>
                        {activePolicyType || "未解析"}
                    </Badge>
                    <span className="text-[10px] text-slate-500">
                        {configuredCount}/{configItems.length} 项
                    </span>
                    {rtc && (
                        <span className="font-mono text-[10px] text-slate-500">
                            RTC {rtc.enabled ? "on" : "off"} · {rtc.action_horizon ?? "—"}/{rtc.prefix_len ?? "—"}/
                            {rtc.execution_horizon ?? "auto"}/{rtc.suffix_len ?? "—"}
                        </span>
                    )}
                </div>
            </div>

            {parseError && <p className="mt-1 text-[11px] text-rose-300">{parseError}</p>}
            {presetLoadError && <p className="mt-1 text-[11px] text-amber-300">{presetLoadError}</p>}
            {policies.length === 0 && <p className="mt-1 text-[11px] text-amber-300">未发现可用策略。</p>}
            {!cfg.endpointSet && <p className="mt-1 text-[11px] text-rose-300">缺端点：{endpointMissing.join("、")}</p>}
            {!configReady && <p className="mt-1 text-[11px] text-rose-300">缺必填：{configMissing.join("、")}</p>}

            <details className="mt-1.5 border-t border-slate-800 pt-1.5 text-[11px]">
                <summary className="cursor-pointer select-none text-slate-500">配置详情 · YAML 与同步摘要</summary>
                <label className="mt-1.5 flex min-w-0 flex-col gap-1">
                    <textarea
                        value={yamlText}
                        onChange={(event) => {
                            edited.current = true;
                            setYamlText(event.target.value);
                        }}
                        rows={10}
                        spellCheck={false}
                        disabled={disabled}
                        className="w-full resize-y rounded border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-[11px] leading-relaxed text-slate-100 disabled:opacity-50"
                    />
                </label>
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <Button
                        size="xs"
                        variant="ghost"
                        disabled={disabled || selectedPreset === null}
                        onClick={() => selectedPreset && applyPreset(selectedPreset)}
                    >
                        恢复预设
                    </Button>
                    <Button size="xs" disabled={disabled || yamlText.trim() === ""} onClick={parseConfig}>
                        解析并同步
                    </Button>
                    <span className="text-[10px] text-slate-500">修改 YAML 后需重新解析。</span>
                </div>
                {configItems.length > 0 && (
                    <div className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-1 border-t border-slate-800 pt-1.5">
                        {configItems.map((item) => (
                            <ConfigValue key={item.key} item={item} value={configValue(item)} />
                        ))}
                    </div>
                )}
            </details>
        </div>
    );
}

/** YAML 解析结果只读展示；避免禁用输入框让人误以为存在第二个编辑入口。 */
function ConfigValue({ item, value }: { item: PolicyConfigItemValue; value: string }) {
    const shown = item.secret ? (item.configured || value !== "" ? "已设置（不回显）" : "未配置") : value || "未配置";
    return (
        <div className={`flex min-w-0 items-baseline gap-1.5${item.multiline ? " col-span-2" : ""}`}>
            <span className="shrink-0 text-[10px] text-slate-500" title={item.label}>
                {item.label}
                {item.required && <span className="text-rose-300"> *</span>}
            </span>
            <span
                className={`min-w-0 truncate font-mono text-[10px] ${
                    shown === "未配置" ? "text-slate-600" : "text-slate-200"
                }`}
                title={shown}
            >
                {shown}
            </span>
        </div>
    );
}
