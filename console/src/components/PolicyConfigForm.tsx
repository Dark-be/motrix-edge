import { useEffect, useRef, useState } from "react";

import { parsePolicyImport } from "../configImport";
import type { usePolicyConfig } from "../hooks/usePolicyConfig";
import { DEFAULT_INFER_PRESET_ID, loadInferPresets, type InferPreset } from "../inferPresets";
import type { PolicyRef } from "../types";
import { Badge, Button, Disclosure } from "./ui";

/** Bump when an old cached policy document is no longer safe to reuse with current presets/schema. */
const POLICY_CONFIG_CACHE_VERSION = 3;

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
    cfg,
    cacheScope,
}: {
    policies: PolicyRef[];
    connected: boolean;
    busy: boolean;
    /** 会话内只读（置灰；要改先退出会话） */
    locked: boolean;
    cfg: ReturnType<typeof usePolicyConfig>;
    /** 按 Edge 地址隔离最近一次已解析的完整策略 YAML。 */
    cacheScope: string;
}) {
    const { activePolicyType, configMissing, configReady, endpointMissing } = cfg;
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
    const cacheKey = `edge-console:policy-config:v${POLICY_CONFIG_CACHE_VERSION}:${cacheScope}`;
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
        if (initialized.current || edited.current || availablePresets.length === 0) return;
        initialized.current = true;
        const cached = localStorage.getItem(cacheKey);
        if (cached && (locked || syncConfig(cached))) {
            setYamlText(cached);
            setSelectedPresetId(availablePresets.find((preset) => preset.source === cached)?.id ?? "");
            return;
        }
        const activePreset = activePolicyType
            ? availablePresets.find((preset) => preset.policyType === activePolicyType)
            : undefined;
        const initial =
            activePreset ??
            availablePresets.find((preset) => preset.id === DEFAULT_INFER_PRESET_ID) ??
            availablePresets[0];
        if (locked) {
            setSelectedPresetId(initial.id);
            setYamlText(initial.source);
            return;
        }
        applyPreset(initial);
        // cfg is intentionally represented by stable setters plus the current policy list.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [policies, locked, activePolicyType, cacheKey, presetCatalog]);

    const configValid = configReady && cfg.endpointSet;
    const presetLabel = selectedPreset?.label ?? (presetCatalog === null ? "加载预设…" : "自定义配置");

    return (
        <Disclosure
            label="推理配置"
            actions={
                <>
                    <span className="max-w-48 truncate text-[10px] text-slate-400" title={presetLabel}>
                        {presetLabel}
                    </span>
                    <Badge tone={configValid ? "blue" : "amber"}>{activePolicyType || "未解析"}</Badge>
                    <Badge tone={configValid ? "green" : "amber"}>{configValid ? "配置有效" : "需补配置"}</Badge>
                </>
            }
        >
            <div className="space-y-1.5">
                <label className="flex min-w-60 flex-1 items-center gap-2">
                    <span className="shrink-0 text-[10px] uppercase tracking-wide text-slate-500">预设</span>
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

                {parseError && <p className="text-[11px] text-rose-300">{parseError}</p>}
                {presetLoadError && <p className="text-[11px] text-amber-300">{presetLoadError}</p>}
                {policies.length === 0 && <p className="text-[11px] text-amber-300">未发现可用策略。</p>}
                {!cfg.endpointSet && <p className="text-[11px] text-rose-300">缺端点：{endpointMissing.join("、")}</p>}
                {!configReady && <p className="text-[11px] text-rose-300">缺必填：{configMissing.join("、")}</p>}

                <label className="flex min-w-0 flex-col gap-1">
                    <span className="text-[10px] uppercase tracking-wide text-slate-500">YAML</span>
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
                <div className="flex flex-wrap items-center gap-1.5">
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
            </div>
        </Disclosure>
    );
}
