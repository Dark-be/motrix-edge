import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import test from "node:test";
import { parse } from "yaml";

import { parsePolicyImport } from "./configImport.ts";

const policies = [
    {
        type: "lerobot-remote",
        class: "Client",
        module: "client",
        config_items: [
            { key: "endpoint", label: "Endpoint", type: "text", required: true },
            { key: "jpeg_quality", label: "JPEG", type: "int", min: 1, max: 100 },
            { key: "encoding", label: "Encoding", type: "text", options: ["raw", "jpeg"] },
            { key: "rtc", label: "RTC", type: "rtc", required: true },
        ],
    },
];

test("policy import includes RTC in the same policy draft", () => {
    assert.deepEqual(
        parsePolicyImport(
            `policy:\n  type: lerobot-remote\n  endpoint: tcp/host:7447\n  encoding: jpeg\n  jpeg_quality: 95\n  rtc:\n    enabled: true\n    action_horizon: 7\n    prefix_len: 0\n    execution_horizon: 7\n    suffix_len: 0\n    aggregate_fn: weighted_average\n`,
            policies
        ),
        {
            policyType: "lerobot-remote",
            draft: {
                endpoint: "tcp/host:7447",
                encoding: "jpeg",
                jpeg_quality: "95",
                rtc: JSON.stringify({
                    enabled: true,
                    action_horizon: 7,
                    prefix_len: 0,
                    execution_horizon: 7,
                    suffix_len: 0,
                    aggregate_fn: "weighted_average",
                }),
            },
        }
    );
});

test("policy import rejects unknown keys instead of silently dropping them", () => {
    assert.throws(() => parsePolicyImport("type: lerobot-remote\nunknown: value", policies), /不支持配置项.*unknown/);
});

test("every checked-in preset has metadata and is accepted as one policy config", () => {
    const presetDir = new URL("../../src/motrix_edge/config/policy/", import.meta.url);
    const common = [
        { key: "infer_freq", label: "Frequency", type: "float" },
        { key: "warmup_required", label: "Warmup", type: "bool" },
        { key: "action_layout", label: "Action", type: "text" },
        { key: "joint_offsets", label: "Offsets", type: "float_vector_map", vector_len: 6 },
        { key: "arm_map", label: "Arms", type: "string_map" },
        { key: "camera_map", label: "Cameras", type: "string_map" },
        { key: "rtc", label: "RTC", type: "rtc", required: true },
    ];
    const configItems = {
        openpi: [
            { key: "host", label: "Host", type: "text" },
            { key: "port", label: "Port", type: "int" },
            ...common,
            { key: "prompt", label: "Prompt", type: "text" },
            { key: "image_size", label: "Image size", type: "int" },
        ],
        "lerobot-remote": [
            { key: "endpoint", label: "Endpoint", type: "text" },
            ...common,
            { key: "deployment", label: "Deployment", type: "text" },
            { key: "semantics", label: "Semantics", type: "text" },
            { key: "task", label: "Task", type: "text" },
            { key: "observation_layout", label: "Observation", type: "text" },
            { key: "mode", label: "Mode", type: "text" },
            { key: "chunk_merge", label: "Merge", type: "text" },
            { key: "encoding", label: "Encoding", type: "text" },
            { key: "jpeg_quality", label: "JPEG", type: "int" },
        ],
    };
    const policyRefs = Object.entries(configItems).map(([type, items]) => ({
        type,
        class: "Client",
        module: "client",
        config_items: items,
    }));
    const ids = new Set();
    const files = readdirSync(presetDir).filter((name) => name.endsWith(".yml"));
    assert.equal(files.length, 2);

    for (const file of files) {
        const source = readFileSync(new URL(file, presetDir), "utf8");
        const document = parse(source);
        assert.equal(typeof document.preset.id, "string");
        assert.equal(typeof document.preset.label, "string");
        assert.ok(!ids.has(document.preset.id), `duplicate preset id ${document.preset.id}`);
        ids.add(document.preset.id);

        const parsed = parsePolicyImport(source, policyRefs);
        assert.equal(parsed.policyType, document.policy.type);
        assert.equal(typeof parsed.draft.action_layout, "string");
        if (document.policy.arm_map !== undefined) {
            assert.deepEqual(JSON.parse(parsed.draft.arm_map), document.policy.arm_map);
        }
        if (document.policy.camera_map !== undefined) {
            assert.deepEqual(JSON.parse(parsed.draft.camera_map), document.policy.camera_map);
        }
        const rtc = JSON.parse(parsed.draft.rtc);
        assert.equal(rtc.enabled, true);
        assert.equal(rtc.prefix_len + rtc.execution_horizon + rtc.suffix_len, rtc.action_horizon);
    }
});

test("default three-button preset uses slow local pose dispatch", () => {
    const preset = parse(
        readFileSync(new URL("../../src/motrix_edge/config/policy/lerobot-remote-default.yml", import.meta.url), "utf8")
    ).policy;
    assert.equal(preset.infer_freq, 0.4);
    assert.equal(preset.endpoint, "tcp/172.29.17.45:7448");
    assert.equal(preset.deployment, "akt-press-three-buttons-v4-first-frame-openloop10-30k");
    assert.equal(preset.action_layout, "pose+gripper");
    assert.deepEqual(preset.arm_map, { right: "observation.state" });
    assert.deepEqual(preset.camera_map, { cam_head: "observation.images.cam_head" });
    assert.deepEqual(preset.joint_offsets.right, [0, 0, 0, 0, 0, 0]);
    assert.deepEqual(preset.rtc, {
        enabled: true,
        action_horizon: 10,
        prefix_len: 0,
        execution_horizon: 10,
        suffix_len: 0,
        aggregate_fn: "weighted_average",
    });
});

test("OpenPI preset has the concise display name", () => {
    const root = parse(
        readFileSync(new URL("../../src/motrix_edge/config/policy/openpi-default.yml", import.meta.url), "utf8")
    );
    assert.equal(root.preset.label, "openpi default");
});
