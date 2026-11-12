import { parse } from "yaml";

import type { PolicyConfigItem, PolicyRef } from "./types";

type YamlMap = Record<string, unknown>;

const RTC_KEYS = [
    "enabled",
    "action_horizon",
    "prefix_len",
    "execution_horizon",
    "suffix_len",
    "aggregate_fn",
] as const;
const RTC_AGGREGATE_FNS = ["weighted_average", "conservative", "average", "latest_only", "continuous"];

const isMap = (value: unknown): value is YamlMap =>
    typeof value === "object" && value !== null && !Array.isArray(value);

const parseMap = (source: string): YamlMap => {
    let value: unknown;
    try {
        value = parse(source);
    } catch (error) {
        throw new Error(`YAML 解析失败：${error instanceof Error ? error.message : String(error)}`);
    }
    if (!isMap(value)) throw new Error("配置必须是 YAML 对象，不能是列表或单个值");
    return value;
};

const normalizeValue = (item: PolicyConfigItem, value: unknown): string => {
    if (value === null || value === undefined) return "";
    if (item.type === "bool") {
        if (typeof value !== "boolean") throw new Error(`${item.key} 必须是 true 或 false`);
        return String(value);
    }
    if (item.type === "int" || item.type === "float") {
        if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${item.key} 必须是数字`);
        if (item.type === "int" && !Number.isInteger(value)) throw new Error(`${item.key} 必须是整数`);
        if (item.min !== undefined && value < item.min) throw new Error(`${item.key} 不能小于 ${item.min}`);
        if (item.max !== undefined && value > item.max) throw new Error(`${item.key} 不能大于 ${item.max}`);
        return String(value);
    }
    if (item.type === "string_map") {
        if (!isMap(value) || Object.keys(value).length === 0) throw new Error(`${item.key} 必须是非空映射`);
        const mapping = Object.fromEntries(
            Object.entries(value).map(([source, target]) => {
                if (source.trim() === "" || typeof target !== "string" || target.trim() === "") {
                    throw new Error(`${item.key} 的键和值必须是非空文本`);
                }
                return [source.trim(), target.trim()];
            })
        );
        return JSON.stringify(mapping);
    }
    if (item.type === "rtc") {
        if (!isMap(value)) throw new Error(`${item.key} 必须是映射`);
        const unknown = Object.keys(value).filter((key) => !(RTC_KEYS as readonly string[]).includes(key));
        if (unknown.length > 0) throw new Error(`未知 RTC 配置项：${unknown.join("、")}`);
        if (typeof value.enabled !== "boolean") throw new Error("rtc.enabled 必须是 true 或 false");
        for (const key of ["action_horizon", "prefix_len", "execution_horizon", "suffix_len"] as const) {
            const raw = value[key];
            if (key === "execution_horizon" && raw === null) continue;
            const positive = key === "action_horizon" || key === "execution_horizon";
            if (typeof raw !== "number" || !Number.isInteger(raw) || raw < (positive ? 1 : 0)) {
                throw new Error(`rtc.${key} 必须是${positive ? "正" : "非负"}整数`);
            }
        }
        if (typeof value.aggregate_fn !== "string" || value.aggregate_fn.trim() === "") {
            throw new Error("rtc.aggregate_fn 必须是非空文本");
        }
        if (!RTC_AGGREGATE_FNS.includes(value.aggregate_fn)) {
            throw new Error(`rtc.aggregate_fn 必须是 ${RTC_AGGREGATE_FNS.join(" / ")} 之一`);
        }
        const horizon = value.action_horizon as number;
        const prefix = value.prefix_len as number;
        const suffix = value.suffix_len as number;
        const execution = value.execution_horizon as number | null;
        if (prefix + suffix >= horizon) throw new Error("rtc.prefix_len + suffix_len 必须小于 action_horizon");
        const effectiveExecution = execution ?? horizon - prefix - suffix;
        if (execution !== null && prefix + execution + suffix > horizon) {
            throw new Error("rtc.prefix_len + execution_horizon + suffix_len 不能超过 action_horizon");
        }
        if (effectiveExecution <= prefix) throw new Error("rtc.execution_horizon 必须大于 prefix_len");
        return JSON.stringify(value);
    }
    if (typeof value !== "string") throw new Error(`${item.key} 必须是文本`);
    if (item.options && !item.options.some((option) => String(option) === value)) {
        throw new Error(`${item.key} 必须是 ${item.options.map(String).join(" / ")} 之一`);
    }
    return value;
};

export function parsePolicyImport(
    source: string,
    policies: PolicyRef[]
): { policyType: string; draft: Record<string, string> } {
    const root = parseMap(source);
    const policy = root.policy === undefined ? root : root.policy;
    if (!isMap(policy)) throw new Error("policy 必须是 YAML 对象");
    const policyType = typeof policy.type === "string" ? policy.type.trim() : "";
    if (!policyType) throw new Error("缺少 policy.type");
    const ref = policies.find((candidate) => candidate.type === policyType);
    if (!ref) throw new Error(`未知策略 ${policyType}（可用：${policies.map((item) => item.type).join("、")}）`);
    const items = ref.config_items ?? [];
    const itemByKey = new Map(items.map((item) => [item.key, item]));
    const unknown = Object.keys(policy).filter((key) => key !== "type" && !itemByKey.has(key));
    if (unknown.length > 0) throw new Error(`当前策略不支持配置项：${unknown.join("、")}`);
    const draft: Record<string, string> = {};
    for (const item of items) {
        if (Object.hasOwn(policy, item.key)) draft[item.key] = normalizeValue(item, policy[item.key]);
    }
    return { policyType, draft };
}
