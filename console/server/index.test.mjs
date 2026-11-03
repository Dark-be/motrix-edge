import assert from "node:assert/strict";
import test from "node:test";

import {
    bindTrustedCaptureBody,
    loadConfig,
    requireAllowedTenant,
    safeReturnTo,
    signIdentityHeaders,
} from "./index.mjs";

test("return path rejects open redirects", () => {
    assert.equal(safeReturnTo("/collect"), "/collect");
    assert.equal(safeReturnTo("https://evil.example"), "/");
    assert.equal(safeReturnTo("//evil.example"), "/");
});

test("tenant allowlist accepts configured tenants and rejects others", () => {
    const allowed = new Set(["tenant-a"]);
    assert.doesNotThrow(() => requireAllowedTenant(allowed, "tenant-a"));
    assert.throws(() => requireAllowedTenant(allowed, "tenant-b"), /Feishu tenant is not allowed/);
    assert.doesNotThrow(() => requireAllowedTenant(new Set(), "development-tenant"));
});

test("trusted LAN capture sync replaces spoofed operator identity", () => {
    const body = bindTrustedCaptureBody(
        Buffer.from(
            JSON.stringify({
                meta: {
                    operator: "伪造",
                    operator_identity: { subject: "attacker" },
                    capture_context: { source: "browser" },
                    task_name: "整理桌面",
                },
            })
        ),
        {
            provider: "feishu",
            subject: "feishu:tenant:ou_user",
            tenant_key: "tenant",
            open_id: "ou_user",
            display_name: "张三",
        },
        "lease-1",
        "2026-11-03T00:00:00.000Z"
    );
    const { meta } = JSON.parse(body.toString("utf8"));
    assert.equal(meta.operator, "张三");
    assert.equal(meta.operator_identity.subject, "feishu:tenant:ou_user");
    assert.equal(meta.capture_context.source, "motrix_console_trusted_network");
    assert.equal(meta.capture_context.lease_id, "lease-1");
    assert.equal(meta.task_name, "整理桌面");
});

test("checked-in Console registry contains only trusted static Edge targets", () => {
    const config = loadConfig(new URL("../console.config.json", import.meta.url));
    assert.equal(config.edges.size, 7);
    for (const edge of config.edges.values()) {
        assert.equal(edge.trustedNetwork, true);
        assert.equal(edge.secret, undefined);
        assert.match(edge.baseUrl, /^http:\/\/10\.30\.(30|40)\./);
    }
});

test("signed identity headers are deterministic for fixed timestamp and nonce", () => {
    const headers = signIdentityHeaders("secret", {
        method: "POST",
        target: "/v1/captures/sync",
        body: Buffer.from("{}"),
        identity: {
            provider: "feishu",
            subject: "feishu:t:ou",
            display_name: "张三",
            tenant_key: "t",
            open_id: "ou",
        },
        timestamp: 1000,
        nonce: "nonce",
    });
    assert.equal(headers["X-Motrix-Subject"], "feishu:t:ou");
    assert.equal(headers["X-Motrix-Signature"], "15e2e0647af95ad1176170bbc5f2af069fe1af3d0dcfc253927c0729d2646df8");
    assert.equal(Buffer.from(headers["X-Motrix-Name"], "base64url").toString(), "张三");
});
