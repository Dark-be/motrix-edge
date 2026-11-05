import assert from "node:assert/strict";
import test from "node:test";

import { isLocalEdgeUrl, LOCAL_EDGE_URL } from "./edgeTargets.ts";

test("localhost Edge is an intrinsic direct target", () => {
    assert.equal(isLocalEdgeUrl(LOCAL_EDGE_URL), true);
    assert.equal(isLocalEdgeUrl("http://localhost:8000/"), true);
    assert.equal(isLocalEdgeUrl("http://127.0.0.1:8000"), false);
    assert.equal(isLocalEdgeUrl("http://localhost:9000"), false);
    assert.equal(isLocalEdgeUrl("http://localhost:8000/v1"), false);
});
