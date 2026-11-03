import { createHmac, createHash, randomBytes } from "node:crypto";
import { createReadStream, existsSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const ROOT = resolve(HERE, "..");
const SIGNATURE_VERSION = "motrix-console-v1";
const MIME = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
};

export function loadConfig(path = process.env.MOTRIX_CONSOLE_CONFIG || join(ROOT, "console.config.json")) {
    const config = JSON.parse(readFileSync(path, "utf8"));
    const tenantSource = process.env.MOTRIX_CONSOLE_FEISHU_ALLOWED_TENANTS
        ? process.env.MOTRIX_CONSOLE_FEISHU_ALLOWED_TENANTS.split(",")
        : config.allowedTenants || [];
    if (!Array.isArray(tenantSource)) throw new Error("allowedTenants must be an array");
    const edges = new Map();
    for (const [id, raw] of Object.entries(config.edges || {})) {
        if (!id || id.includes("/")) throw new Error(`invalid Edge id: ${id}`);
        const baseUrl = validatedBaseUrl(raw.baseUrl, id);
        const secret = raw.sharedSecret || process.env[raw.sharedSecretEnv || ""];
        if (!secret) throw new Error(`Edge ${id} shared secret is not configured`);
        edges.set(id, { id, label: raw.label || id, baseUrl, secret });
    }
    return {
        host: config.host || "0.0.0.0",
        port: Number(config.port || 5174),
        frontendUrl: config.frontendUrl,
        redirectUri: config.redirectUri,
        cookieSecure: Boolean(config.cookieSecure),
        sessionTtl: Number(config.sessionTtl || 28800),
        stateTtl: Number(config.stateTtl || 600),
        appId: process.env.MOTRIX_CONSOLE_FEISHU_APP_ID,
        appSecret: process.env.MOTRIX_CONSOLE_FEISHU_APP_SECRET,
        allowedTenants: new Set(tenantSource.map((value) => String(value).trim()).filter(Boolean)),
        edges,
    };
}

export function requireAllowedTenant(allowedTenants, tenantKey) {
    if (allowedTenants.size && !allowedTenants.has(tenantKey)) throw httpError(403, "Feishu tenant is not allowed");
}

export function signIdentityHeaders(secret, { method, target, body, identity, timestamp, nonce }) {
    const subject = String(identity.subject);
    const name = String(identity.display_name);
    validateHeaderText(subject, "subject");
    validateHeaderText(name, "display_name");
    const claims = { ...identity, provider: "feishu", subject, display_name: name };
    const encodedName = base64url(Buffer.from(name));
    const encodedIdentity = base64url(Buffer.from(stableStringify(claims)));
    const now = Number(timestamp ?? Math.floor(Date.now() / 1000));
    const requestNonce = nonce || base64url(randomBytes(18));
    const bodyHash = createHash("sha256").update(body).digest("hex");
    const canonical = [
        SIGNATURE_VERSION,
        String(now),
        requestNonce,
        method.toUpperCase(),
        target,
        bodyHash,
        subject,
        name,
        encodedIdentity,
    ].join("\n");
    const signature = createHmac("sha256", secret).update(canonical).digest("hex");
    return {
        "X-Motrix-Subject": subject,
        "X-Motrix-Name": encodedName,
        "X-Motrix-Timestamp": String(now),
        "X-Motrix-Nonce": requestNonce,
        "X-Motrix-Signature": signature,
        "X-Motrix-Identity": encodedIdentity,
    };
}

export function safeReturnTo(value) {
    return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") ? value : "/";
}

export function createConsoleServer(config = loadConfig()) {
    requireConfig(config);
    const states = new Map();
    const sessions = new Map();

    return createServer(async (req, res) => {
        try {
            const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
            prune(states);
            prune(sessions);
            if (url.pathname === "/api/auth") return json(res, 200, { enabled: true, provider: "feishu" });
            if (url.pathname === "/api/auth/feishu/login") {
                const state = base64url(randomBytes(32));
                states.set(state, {
                    expiresAt: now() + config.stateTtl,
                    returnTo: safeReturnTo(url.searchParams.get("return_to")),
                });
                const target = new URL("https://accounts.feishu.cn/open-apis/authen/v1/authorize");
                target.searchParams.set("app_id", config.appId);
                target.searchParams.set("redirect_uri", config.redirectUri);
                target.searchParams.set("response_type", "code");
                target.searchParams.set("state", state);
                return redirect(res, 307, target.toString());
            }
            if (url.pathname === "/api/auth/feishu/callback") {
                const state = url.searchParams.get("state");
                const transaction = states.get(state);
                states.delete(state);
                if (!transaction) return json(res, 403, { detail: "invalid or expired OAuth state" });
                const identity = await exchangeFeishuUser(config, url.searchParams.get("code"));
                const sessionId = base64url(randomBytes(32));
                sessions.set(sessionId, { expiresAt: now() + config.sessionTtl, identity });
                res.setHeader("Set-Cookie", sessionCookie("motrix_console_session", sessionId, config));
                return redirect(res, 303, `${config.frontendUrl.replace(/\/$/, "")}${transaction.returnTo}`);
            }
            if (url.pathname === "/api/auth/logout" && req.method === "POST") {
                sessions.delete(cookie(req, "motrix_console_session"));
                res.setHeader("Set-Cookie", "motrix_console_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0");
                return json(res, 200, { authenticated: false });
            }
            if (url.pathname === "/api/auth/me") {
                const session = requireSession(req, sessions);
                return json(res, 200, { authenticated: true, user: session.identity });
            }
            if (url.pathname === "/api/edges") {
                requireSession(req, sessions);
                return json(res, 200, { edges: [...config.edges.values()].map(({ id, label }) => ({ id, label })) });
            }
            const match = url.pathname.match(/^\/api\/edges\/([^/]+)(\/v1(?:\/.*)?)$/);
            if (match) {
                const session = requireSession(req, sessions);
                const edge = config.edges.get(decodeURIComponent(match[1]));
                if (!edge) return json(res, 404, { detail: "edge not found" });
                const body = await readBody(req);
                const target = `${match[2]}${url.search}`;
                const headers = forwardHeaders(req.headers);
                Object.assign(
                    headers,
                    signIdentityHeaders(edge.secret, { method: req.method, target, body, identity: session.identity })
                );
                const upstream = await fetch(`${edge.baseUrl}${target}`, {
                    method: req.method,
                    headers,
                    body: body.length ? body : undefined,
                });
                res.writeHead(upstream.status, responseHeaders(upstream.headers));
                return res.end(Buffer.from(await upstream.arrayBuffer()));
            }
            return serveStatic(url.pathname, res);
        } catch (error) {
            return json(res, error.status || 500, { detail: error.message || "internal error" });
        }
    });
}

async function exchangeFeishuUser(config, code) {
    if (!code) throw httpError(400, "Feishu callback requires code");
    const token = await fetch("https://open.feishu.cn/open-apis/authen/v2/oauth/token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            grant_type: "authorization_code",
            client_id: config.appId,
            client_secret: config.appSecret,
            code,
            redirect_uri: config.redirectUri,
        }),
    });
    const tokenData = await token.json();
    if (!token.ok || !tokenData.access_token) throw httpError(502, "Feishu token exchange failed");
    const user = await fetch("https://open.feishu.cn/open-apis/authen/v1/user_info", {
        headers: { Authorization: `Bearer ${tokenData.access_token}` },
    });
    const payload = await user.json();
    const data = payload.data || payload;
    if (!user.ok || !data.open_id || !data.tenant_key || !data.name) throw httpError(502, "Feishu user info failed");
    const tenantKey = String(data.tenant_key).trim();
    const openId = String(data.open_id).trim();
    const displayName = String(data.name).trim();
    if (!tenantKey || !openId || !displayName) throw httpError(502, "Feishu user info failed");
    requireAllowedTenant(config.allowedTenants, tenantKey);
    return {
        provider: "feishu",
        subject: `feishu:${tenantKey}:${openId}`,
        tenant_key: tenantKey,
        open_id: openId,
        display_name: displayName,
        ...(data.union_id ? { union_id: data.union_id } : {}),
        ...(data.user_id ? { user_id: data.user_id } : {}),
    };
}

function serveStatic(pathname, res) {
    const dist = resolve(ROOT, "dist");
    const requested = normalize(pathname).replace(/^(\.\.(\/|\\|$))+/, "");
    let path = resolve(dist, `.${requested}`);
    if (!path.startsWith(dist) || !existsSync(path) || statSync(path).isDirectory()) path = join(dist, "index.html");
    if (!existsSync(path)) return json(res, 503, { detail: "Console frontend is not built; run npm run build" });
    res.writeHead(200, {
        "Content-Type": MIME[extname(path)] || "application/octet-stream",
        "Cache-Control": path.endsWith("index.html") ? "no-store" : "public, max-age=31536000, immutable",
    });
    createReadStream(path).pipe(res);
}

function requireSession(req, sessions) {
    const session = sessions.get(cookie(req, "motrix_console_session"));
    if (!session) throw httpError(401, "Feishu login required");
    return session;
}

function requireConfig(config) {
    if (!config.appId || !config.appSecret || !config.redirectUri || !config.frontendUrl)
        throw new Error("Console Feishu configuration is incomplete");
}

function validatedBaseUrl(value, id) {
    const parsed = new URL(value);
    if (
        !/^https?:$/.test(parsed.protocol) ||
        parsed.username ||
        parsed.password ||
        parsed.pathname !== "/" ||
        parsed.search ||
        parsed.hash
    )
        throw new Error(`invalid baseUrl for Edge ${id}`);
    return parsed.toString().replace(/\/$/, "");
}

function stableStringify(value) {
    if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
    if (value && typeof value === "object")
        return `{${Object.keys(value)
            .sort()
            .map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`)
            .join(",")}}`;
    return JSON.stringify(value);
}

function forwardHeaders(headers) {
    const result = {};
    for (const key of ["accept", "content-type", "x-correlation-id", "x-lease-id"])
        if (headers[key]) result[key] = headers[key];
    return result;
}

function responseHeaders(headers) {
    const result = {};
    for (const key of ["cache-control", "content-type", "x-correlation-id"])
        if (headers.get(key)) result[key] = headers.get(key);
    return result;
}

function readBody(req) {
    return new Promise((resolveBody, reject) => {
        const chunks = [];
        let size = 0;
        req.on("data", (chunk) => {
            size += chunk.length;
            if (size > 10 * 1024 * 1024) reject(httpError(413, "request body too large"));
            else chunks.push(chunk);
        });
        req.on("end", () => resolveBody(Buffer.concat(chunks)));
        req.on("error", reject);
    });
}

function cookie(req, name) {
    for (const item of String(req.headers.cookie || "").split(";")) {
        const [key, ...parts] = item.trim().split("=");
        if (key === name) return decodeURIComponent(parts.join("="));
    }
    return "";
}

function sessionCookie(name, value, config) {
    return `${name}=${encodeURIComponent(value)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${config.sessionTtl}${
        config.cookieSecure ? "; Secure" : ""
    }`;
}

function prune(store) {
    const current = now();
    for (const [key, value] of store) if (value.expiresAt <= current) store.delete(key);
}

function validateHeaderText(value, field) {
    if (!value || /[\r\n]/.test(value)) throw new Error(`invalid identity ${field}`);
}

function base64url(value) {
    return Buffer.from(value).toString("base64url");
}

function now() {
    return Math.floor(Date.now() / 1000);
}

function httpError(status, message) {
    return Object.assign(new Error(message), { status });
}

function json(res, status, value) {
    const body = Buffer.from(JSON.stringify(value));
    res.writeHead(status, {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": body.length,
        "Cache-Control": "no-store",
    });
    res.end(body);
}

function redirect(res, status, location) {
    res.writeHead(status, { Location: location, "Cache-Control": "no-store" });
    res.end();
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
    const config = loadConfig();
    createConsoleServer(config).listen(config.port, config.host, () => {
        console.log(`Motrix Console listening on http://${config.host}:${config.port}`);
    });
}
