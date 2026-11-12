import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
    bindTrustedCaptureBody,
    loadConfig,
    loadPolicyPresets,
    parseServerArgs,
    requireAllowedTenant,
    safeReturnTo,
    signIdentityHeaders,
} from "./index.mjs";

test("runtime policy directory replaces built-in presets", (t) => {
    const directory = mkdtempSync(join(tmpdir(), "motrix-policy-presets-"));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    writeFileSync(
        join(directory, "light-switch.yml"),
        `preset:
  id: lerobot-remote-light-switch
  label: turn on light switch
policy:
  type: lerobot-remote
  deployment: akt-turn-on-light-switch-v3-first-frame-openloop4-30k
  task: turn on the light switch
  rtc:
    action_horizon: 4
    execution_horizon: 4
`
    );
    writeFileSync(
        join(directory, "openpi.yml"),
        `preset:
  id: openpi-default
  label: site openpi
policy:
  type: openpi
`
    );

    const presets = loadPolicyPresets(directory);
    assert.equal(presets.length, 2);
    assert.equal(presets.find((preset) => preset.id === "openpi-default").label, "site openpi");
    const lightSwitch = presets.find((preset) => preset.id === "lerobot-remote-light-switch");
    assert.equal(lightSwitch.policyType, "lerobot-remote");
    assert.match(lightSwitch.source, /action_horizon: 4/);
});

test("built-in policy presets are used only when the runtime directory is absent", () => {
    const missing = join(tmpdir(), `missing-motrix-policy-${process.pid}-${Date.now()}`);
    const presets = loadPolicyPresets(missing);
    assert.deepEqual(presets.map((preset) => preset.id).sort(), ["lerobot-remote-default", "openpi-default"]);
});

test("server CLI separates listen overrides from the public OAuth URL", () => {
    assert.deepEqual(
        parseServerArgs(["--host", "127.0.0.1", "--port", "5183", "--public-url", "http://127.0.0.1:5183"]),
        {
            host: "127.0.0.1",
            port: 5183,
            frontendUrl: "http://127.0.0.1:5183",
            redirectUri: "http://127.0.0.1:5183/api/auth/feishu/callback",
        }
    );
    assert.deepEqual(parseServerArgs(["--port=6000", "--public-url", "http://127.0.0.1:6000"]), {
        port: 6000,
        frontendUrl: "http://127.0.0.1:6000",
        redirectUri: "http://127.0.0.1:6000/api/auth/feishu/callback",
    });
    assert.deepEqual(parseServerArgs([]), {});
    assert.throws(() => parseServerArgs(["--port", "0"]), /invalid Console port/);
    assert.throws(() => parseServerArgs(["--unknown"]), /unknown Console option/);
    assert.throws(() => parseServerArgs(["--public-url", "http://127.0.0.1:5183/path"]), /invalid baseUrl/);
});

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
    assert.deepEqual(
        [...config.harnessProviders.values()],
        [
            {
                id: "rpent-local",
                label: "RPent · Motrix Edge",
                kind: "rpent",
                baseUrl: "http://127.0.0.1:8765",
            },
        ]
    );
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
