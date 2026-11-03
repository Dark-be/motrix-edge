/**
 * 机械臂正解（前向运动学）——**纯前端近似显示**用。
 *
 * DH 参数与关节限位取自 robot-pipeline 的 Piper 运动学
 * （`robot-pipeline/src/robot/kinematics/piper.py`），保证网页里画的姿态与真机姿态**同源**；
 * 这里只做展示（点 / 线连杆 + 期望轨迹），不参与任何下发（下发走 `robot execute`，语义由
 * 机器人侧解释）。
 *
 * 坐标系：DH 基座系（z 轴沿关节 1 轴向上，与机器人基座系一致）。渲染侧把整组内容旋转
 * 到 three.js 的 y-up（见 armScene）。
 */

/** 单臂自由度数（Piper 六轴）。 */
export const ARM_DOF = 6;

export type Vec3 = [number, number, number];
/** 3×3 旋转矩阵（行主序 9 元） */
type Mat3 = [number, number, number, number, number, number, number, number, number];

export interface Pose {
    R: Mat3;
    p: Vec3;
}

/** Modified DH 参数：`[alpha, a, d, theta_offset]`（与 Piper 同源）。 */
export const PIPER_DH: readonly (readonly [number, number, number, number])[] = [
    [0.0, 0.0, 0.123, 0.0],
    [-Math.PI / 2, 0.0, 0.0, (-172.22 * Math.PI) / 180.0],
    [0.0, 0.28503, 0.0, (-102.78 * Math.PI) / 180.0],
    [Math.PI / 2, -0.021984, 0.25075, 0.0],
    [-Math.PI / 2, 0.0, 0.0, 0.0],
    [Math.PI / 2, 0.0, 0.091, 0.0],
];

/** 关节软限位（弧度，`[min, max]`；= SDK 机型限位内缩 0.001 rad，与 robot-pipeline 同源）——手动滑块的量程。 */
export const PIPER_JOINT_LIMITS: readonly (readonly [number, number])[] = [
    [-2.616994, 2.616994],
    [0.0, 3.140593],
    [-2.96606, 0.0],
    [-1.74433, 1.74433],
    [-1.22073, 1.22073],
    [-1.7443, 1.7443],
];

/** 第 `i` 关节限位（弧度）；超出 DH 表长度时回落到 ±180°。 */
export function jointLimit(i: number): [number, number] {
    const limit = PIPER_JOINT_LIMITS[i];
    return limit ? [limit[0], limit[1]] : [-Math.PI, Math.PI];
}

const IDENTITY: Mat3 = [1, 0, 0, 0, 1, 0, 0, 0, 1];

function add(a: Vec3, b: Vec3): Vec3 {
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

/** 行主序 3×3 相乘：`a · b`。 */
function mul3(a: Mat3, b: Mat3): Mat3 {
    const out = new Array<number>(9);
    for (let r = 0; r < 3; r += 1) {
        for (let c = 0; c < 3; c += 1) {
            out[r * 3 + c] = a[r * 3] * b[c] + a[r * 3 + 1] * b[3 + c] + a[r * 3 + 2] * b[6 + c];
        }
    }
    return out as Mat3;
}

/** `R · v`。 */
function mulVec(R: Mat3, v: Vec3): Vec3 {
    return [
        R[0] * v[0] + R[1] * v[1] + R[2] * v[2],
        R[3] * v[0] + R[4] * v[1] + R[5] * v[2],
        R[6] * v[0] + R[7] * v[1] + R[8] * v[2],
    ];
}

/** Modified DH 单连杆齐次矩阵（`Rx(alpha)·Tx(a)·Rz(theta)·Tz(d)`）拆成 `(R, p)`。 */
function dhMatrix(alpha: number, a: number, d: number, theta: number): Pose {
    const ca = Math.cos(alpha);
    const sa = Math.sin(alpha);
    const ct = Math.cos(theta);
    const st = Math.sin(theta);
    return {
        R: [ct, -st, 0, st * ca, ct * ca, -sa, st * sa, ct * sa, ca],
        p: [a, -d * sa, d * ca],
    };
}

/** 把任意长度的关节值适配到单臂自由度（不足补 0、超出截断）。 */
export function fitJoints(values: readonly number[] | null | undefined): number[] {
    const out = new Array<number>(ARM_DOF).fill(0);
    if (!values) return out;
    for (let i = 0; i < ARM_DOF && i < values.length; i += 1) out[i] = values[i];
    return out;
}

/** 正解链：`result[0]` = 基座系，`result[i]` = 关节 i 原点系，共 `DOF + 1` 个。 */
export function fkChain(joints: readonly number[]): Pose[] {
    const chain: Pose[] = [{ R: IDENTITY, p: [0, 0, 0] }];
    let acc = chain[0];
    for (let i = 0; i < PIPER_DH.length; i += 1) {
        const [alpha, a, d, offset] = PIPER_DH[i];
        const link = dhMatrix(alpha, a, d, (joints[i] ?? 0) + offset);
        acc = { R: mul3(acc.R, link.R), p: add(mulVec(acc.R, link.p), acc.p) };
        chain.push(acc);
    }
    return chain;
}

/** 末端（法兰）位姿：`xyz + rpy`（米 / 弧度）——与 `/v1/preview` 的 `pose` 同约定。 */
export function fkEEF(joints: readonly number[]): { xyz: Vec3; rpy: Vec3; chain: Pose[] } {
    const chain = fkChain(joints);
    const end = chain[chain.length - 1];
    const R = end.R;
    // 行主序 R → rpy（ZYX 内旋约定）
    const pitch = Math.atan2(-R[6], Math.hypot(R[0], R[3]));
    const roll = Math.atan2(R[7], R[8]);
    const yaw = Math.atan2(R[3], R[0]);
    return { xyz: end.p, rpy: [roll, pitch, yaw], chain };
}

/** 关节原点位置（画连杆用）：长度 = `DOF + 1`（基座 → 法兰）。 */
export function jointPositions(joints: readonly number[]): Vec3[] {
    return fkChain(joints).map((pose) => pose.p);
}

/** 扁平值按 `arms` 切成每臂一段（除不尽则整条当一臂）——与 preview 的分行口径一致。 */
export function splitByArms(values: readonly number[] | null | undefined, arms: readonly string[]): number[][] {
    if (!values || values.length === 0) return [];
    if (arms.length <= 1 || values.length % arms.length !== 0) return [Array.from(values)];
    const per = values.length / arms.length;
    return arms.map((_, i) => Array.from(values.slice(i * per, (i + 1) * per)));
}

/** 臂名缩写（`left` → `L`，`right` → `R`，其它取首字母大写；无名 → `A`）。 */
export function armShort(name: string, index: number): string {
    const text = (name || "").trim();
    if (!text) return String.fromCharCode(65 + (index % 26));
    return text[0].toUpperCase();
}

/** 关节标签（`L0…L5` / `R0…R5`）：手动滑块与 3D 关节球一致。 */
export function jointLabels(armName: string, index: number): string[] {
    const short = armShort(armName, index);
    return Array.from({ length: ARM_DOF }, (_, i) => `${short}${i}`);
}
