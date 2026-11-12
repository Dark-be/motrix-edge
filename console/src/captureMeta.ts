/** 开始一轮人工采集前必须已同步到机器人进程的元信息。 */
export const REQUIRED_CAPTURE_META = [
    { key: "operator", label: "采集人" },
    { key: "task_name", label: "采集任务" },
] as const;

/** 返回缺失的必填项；空白字符串与非字符串都视为未填写。 */
export function missingCaptureMeta(meta: Record<string, unknown>): string[] {
    return REQUIRED_CAPTURE_META.filter(({ key }) => {
        const value = meta[key];
        return typeof value !== "string" || value.trim() === "";
    }).map(({ label }) => label);
}

/** 组装同步请求的 meta：包含登录态注入的 operator，并统一裁剪空白值。 */
export function captureSyncMeta(selection: Record<string, string>): Record<string, string> {
    const meta: Record<string, string> = {};
    for (const [key, value] of Object.entries(selection)) {
        const trimmed = value.trim();
        if (trimmed !== "") meta[key] = trimmed;
    }
    return meta;
}
