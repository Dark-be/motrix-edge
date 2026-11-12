import assert from "node:assert/strict";
import test from "node:test";

import { captureSyncMeta, missingCaptureMeta } from "./captureMeta.ts";

test("capture sync includes the authenticated operator used by required-field validation", () => {
    const effectiveSelection = {
        operator: " 张三 ",
        task_name: " 桌面整理 ",
        optional: "   ",
    };

    assert.deepEqual(missingCaptureMeta(effectiveSelection), []);
    assert.deepEqual(captureSyncMeta(effectiveSelection), {
        operator: "张三",
        task_name: "桌面整理",
    });
});
