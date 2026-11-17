/** 浏览器所在机器的 Edge：不经过 Console BFF，也不要求出现在服务端静态注册表。 */
export const LOCAL_EDGE_URL = "http://localhost:8000";

/** 旧版把 Console BFF 的 Edge 代理根地址持久化为连接目标；新版只允许直连真实 Edge。 */
export function isLegacyEdgeProxyUrl(value: string, consoleOrigin: string): boolean {
    try {
        const origin = new URL(consoleOrigin).origin;
        const url = new URL(value.trim(), `${origin}/`);
        return url.origin === origin && /^\/api\/edges\/[^/]+\/?$/.test(url.pathname);
    } catch {
        return false;
    }
}

export function isLocalEdgeUrl(value: string): boolean {
    try {
        const url = new URL(value.trim());
        return url.protocol === "http:" && url.hostname === "localhost" && url.port === "8000" && url.pathname === "/";
    } catch {
        return false;
    }
}
