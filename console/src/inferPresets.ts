type YamlMap = Record<string, unknown>;

export type InferPreset = {
    id: string;
    label: string;
    policyType: string;
    source: string;
};

const isMap = (value: unknown): value is YamlMap =>
    typeof value === "object" && value !== null && !Array.isArray(value);

export async function loadInferPresets(): Promise<InferPreset[]> {
    const response = await fetch("/api/policy-presets", { credentials: "same-origin" });
    if (!response.ok) throw new Error(`加载策略预设失败：HTTP ${response.status}`);
    const payload: unknown = await response.json();
    if (!isMap(payload) || !Array.isArray(payload.presets)) throw new Error("策略预设接口返回格式错误");
    const presets = payload.presets.map((value, index) => {
        if (!isMap(value)) throw new Error(`策略预设接口第 ${index + 1} 项格式错误`);
        const id = typeof value.id === "string" ? value.id.trim() : "";
        const label = typeof value.label === "string" ? value.label.trim() : "";
        const policyType = typeof value.policyType === "string" ? value.policyType.trim() : "";
        const source = typeof value.source === "string" ? value.source : "";
        if (!id || !label || !policyType || !source) throw new Error(`策略预设接口第 ${index + 1} 项缺少字段`);
        return { id, label, policyType, source };
    });
    if (new Set(presets.map((preset) => preset.id)).size !== presets.length) {
        throw new Error("策略预设接口返回了重复 preset.id");
    }
    return presets;
}
