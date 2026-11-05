/** 浏览器本机 Edge：不经过 Console BFF，也不要求出现在服务端静态注册表。 */
export const LOCAL_EDGE_URL = "http://localhost:8000";

export function isLocalEdgeUrl(value: string): boolean {
    try {
        const url = new URL(value.trim());
        return url.protocol === "http:" && url.hostname === "localhost" && url.port === "8000" && url.pathname === "/";
    } catch {
        return false;
    }
}
