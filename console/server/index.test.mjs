import assert from "node:assert/strict";
import test from "node:test";

import { requireAllowedTenant, safeReturnTo, signIdentityHeaders } from "./index.mjs";

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
