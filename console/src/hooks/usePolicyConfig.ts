import { useEffect, useMemo, useState } from "react";

import type { InferStatus, PolicyConfigItemValue, PolicyRef } from "../types";

/**
 * 策略配置模型：schema + 当前值 + 缺失必填项 + 端点项 + 待下发 payload。
 *
 * 进入推理前的**门控**（缺必填项 / 端点没填齐 → 不能进入）与会话内的**只读回显**用的是同一份
 * 模型，故抽成 hook：`SessionDeck` 用它门控「进入并预热」按钮，`PolicyConfigForm` 用它渲染表单。
 *
 * **策略配置是普通配置项**（没有「连接后锁定」的额外轴）：进入会话时随 body 下发一次，
 * 会话内也可经 `POST /v1/infers/config` 热改（`runtime=True` 的键立刻生效）。
 */
export function usePolicyConfig({
    infers,
    policies,
    sessionType,
    nodeState,
}: {
    infers: InferStatus | null;
    policies: PolicyRef[];
    sessionType: string | null;
    nodeState: string | null;
}) {
    const [selectedPolicy, setSelectedPolicy] = useState("");
    const [draft, setDraft] = useState<Record<string, string>>({});

    const inInfer = nodeState === "active" && sessionType === "infer";

    // 后端值回填草稿（只在用户尚未编辑该项时填，避免覆盖正在输入的内容）：进入会话后展示
    // 已生效值，未进入会话时用 schema 缺省值（见下面 configItems 的兜底）。
    useEffect(() => {
        const values = infers?.policy_config?.values;
        if (!values) return;
        setDraft((prev) => {
            const next = { ...prev };
            for (const [key, value] of Object.entries(values)) {
                if (next[key] === undefined && value !== null && value !== undefined) next[key] = String(value);
            }
            return next;
        });
    }, [infers?.policy_config]);

    const activePolicyType = (inInfer ? infers?.policy ?? selectedPolicy : selectedPolicy) || "";
    const sessionConfig = infers?.policy_config ?? null;
    const useSessionConfig = inInfer && sessionConfig !== null && sessionConfig.policy_type === activePolicyType;
    const selectedPolicyRef = useMemo(
        () => policies.find((p) => p.type === activePolicyType) ?? null,
        [policies, activePolicyType]
    );
    /** 配置项清单：会话内用后端 schema（含已生效值），未进入会话用所选策略的 schema + 缺省值。 */
    const configItems: PolicyConfigItemValue[] = useMemo(
        () =>
            useSessionConfig
                ? sessionConfig.items
                : (selectedPolicyRef?.config_items ?? []).map((item) => ({ ...item, value: item.default ?? null })),
        [useSessionConfig, sessionConfig, selectedPolicyRef]
    );

    /** 当前输入值（草稿优先 → 后端值 → schema 缺省）。 */
    const configValue = (item: PolicyConfigItemValue): string => {
        const editable = draft[item.key];
        if (editable !== undefined) return editable;
        const serverValue = useSessionConfig ? sessionConfig.values[item.key] : item.value;
        return serverValue === null || serverValue === undefined ? "" : String(serverValue);
    };

    const setField = (key: string, value: string) => setDraft((prev) => ({ ...prev, [key]: value }));

    /** 缺失的必填项：会话内用后端结论，未进入会话前端自查。 */
    const configMissing = useSessionConfig
        ? sessionConfig.missing
        : configItems.filter((item) => item.required && configValue(item).trim() === "").map((item) => item.key);
    const configReady = configMissing.length === 0;

    /** 端点项（`group="endpoint"`：只有需端点的策略有，不硬编码 host / port 键名）。 */
    const endpointItems = configItems.filter((item) => item.group === "endpoint");
    const endpointFilled = (item: PolicyConfigItemValue): boolean => {
        const raw = configValue(item).trim();
        if (raw === "") return false;
        if (item.type !== "int" && item.type !== "float") return true;
        const value = Number(raw);
        if (!Number.isFinite(value)) return false;
        return (item.min === undefined || value >= item.min) && (item.max === undefined || value <= item.max);
    };
    const endpointMissing = endpointItems.filter((item) => !endpointFilled(item)).map((item) => item.key);
    const endpointSet = endpointMissing.length === 0;

    /** 是否需要 prompt（语言条件策略，如 openpi）；lerobot-act → false，不参与门控。 */
    const requiresPrompt = useSessionConfig
        ? sessionConfig.requires_prompt
        : configItems.some((item) => item.key === "prompt" && item.required);

    /** prompt 输入框的当前值（录制时作为 episode 的 task_name）。 */
    const promptValue = (() => {
        const item = configItems.find((i) => i.key === "prompt");
        return item ? configValue(item) : infers?.prompt ?? "";
    })();

    /** 待下发配置（只传非空值；int / float 转数字）——进入会话时随 body 一次性下发。 */
    const configPayload = (): Record<string, unknown> => {
        const body: Record<string, unknown> = {};
        for (const item of configItems) {
            const raw = configValue(item).trim();
            if (raw === "") continue;
            body[item.key] =
                item.type === "int" || item.type === "float"
                    ? Number(raw)
                    : item.type === "bool"
                    ? raw === "true"
                    : raw;
        }
        return body;
    };

    return {
        inInfer,
        selectedPolicy,
        setSelectedPolicy,
        activePolicyType,
        configItems,
        configValue,
        setField,
        configMissing,
        configReady,
        endpointMissing,
        endpointSet,
        requiresPrompt,
        promptValue,
        configPayload,
    };
}
