import assert from "node:assert/strict";
import test from "node:test";

import { isLegacyEdgeProxyUrl, isLocalEdgeUrl, LOCAL_EDGE_URL } from "./edgeTargets.ts";

test("localhost Edge is an intrinsic direct target", () => {
    assert.equal(isLocalEdgeUrl(LOCAL_EDGE_URL), true);
    assert.equal(isLocalEdgeUrl("http://localhost:8000/"), true);
    assert.equal(isLocalEdgeUrl("http://127.0.0.1:8000"), false);
    assert.equal(isLocalEdgeUrl("http://localhost:9000"), false);
    assert.equal(isLocalEdgeUrl("http://localhost:8000/v1"), false);
});

test("legacy same-origin Edge proxy targets are recognized for cache migration", () => {
    const origin = "http://10.30.20.64:5174";
    assert.equal(isLegacyEdgeProxyUrl("/api/edges/piper05", origin), true);
    assert.equal(isLegacyEdgeProxyUrl(`${origin}/api/edges/piper05`, origin), true);
    assert.equal(isLegacyEdgeProxyUrl("http://localhost:8000", origin), false);
    assert.equal(isLegacyEdgeProxyUrl("http://10.30.40.58:8000", origin), false);
    assert.equal(isLegacyEdgeProxyUrl("http://other.example/api/edges/piper05", origin), false);
});
