import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

import { ARM_DOF, fitJoints, fkChain, type Vec3 } from "./kinematics";

/** 配色（随主题切换；数值取自 index.css 的令牌语义）。 */
export interface ScenePalette {
    link: number;
    joint: number;
    eef: number;
    base: number;
    grid: number;
    grid2: number;
    ghost: number;
    trajectory: number;
    trail: number;
}

export const PALETTES: Record<"light" | "dark", ScenePalette> = {
    light: {
        link: 0x5c6673,
        joint: 0x99a2af,
        eef: 0xc2410c,
        base: 0xd9dee6,
        grid: 0xdfe4ea,
        grid2: 0xc3cad6,
        ghost: 0x0ea5e9,
        trajectory: 0x15803d,
        trail: 0xb45309,
    },
    dark: {
        link: 0x94a3b8,
        joint: 0x475569,
        eef: 0x38bdf8,
        base: 0x1e293b,
        grid: 0x1e293b,
        grid2: 0x334155,
        ghost: 0x38bdf8,
        trajectory: 0x34d399,
        trail: 0xfbbf24,
    },
};

/** 单臂状态：关节值 + 可选夹爪（0 = 闭合，1 = 张开）。 */
export interface ArmState {
    joints: number[];
    gripper: number | null;
}

/** 一条折线（关节原点 / 末端轨迹），`armIndex` 决定挂在哪条臂的基座系下。 */
export interface Polyline {
    armIndex: number;
    points: Vec3[];
    color: number;
}

/**
 * 双臂安装间距（米）：真机实测——左臂基座在原点，右臂沿世界 y 偏 -0.7（世界系 z 向上）。
 * 两臂朝向一致（基座系 x 轴 = 世界 +x），不做额外偏转。
 */
const ARM_SEPARATION = 0.7;

/**
 * 默认机位 / 视点（three 世界系，y-up）：两臂沿世界 z 排开（世界 y 的 -0.7 = three z +0.7），
 * 视线取在 +x 一侧（正面），两臂并排不互相遮挡；视点取两臂中点。
 */
const DEFAULT_CAMERA = new THREE.Vector3(1.0, 0.66, 0.12);
const DEFAULT_TARGET = new THREE.Vector3(0, 0.18, ARM_SEPARATION / 2);

/** 单条机械臂的可视化刚体（连杆圆柱 + 关节球 + 夹爪 + 基座）。 */
class ArmRig {
    readonly group = new THREE.Group();
    private links: THREE.Mesh[] = [];
    private joints: THREE.Mesh[] = [];
    private fingers: THREE.Mesh[] = [];
    private eef: THREE.Mesh;
    private linkMat: THREE.MeshStandardMaterial;
    private jointMat: THREE.MeshStandardMaterial;
    private eefMat: THREE.MeshStandardMaterial;

    constructor(opacity: number, palette: ScenePalette, transparent: boolean) {
        this.linkMat = new THREE.MeshStandardMaterial({
            color: palette.link,
            roughness: 0.55,
            metalness: 0.15,
            transparent,
            opacity,
        });
        this.jointMat = new THREE.MeshStandardMaterial({
            color: palette.joint,
            roughness: 0.5,
            metalness: 0.3,
            transparent,
            opacity,
        });
        this.eefMat = new THREE.MeshStandardMaterial({
            color: palette.eef,
            roughness: 0.35,
            metalness: 0.2,
            transparent,
            opacity,
        });

        const linkGeo = new THREE.CylinderGeometry(0.012, 0.012, 1, 10);
        for (let i = 0; i < ARM_DOF; i += 1) {
            const mesh = new THREE.Mesh(linkGeo, this.linkMat);
            this.links.push(mesh);
            this.group.add(mesh);
        }
        const jointGeo = new THREE.SphereGeometry(0.017, 12, 10);
        for (let i = 0; i < ARM_DOF; i += 1) {
            const mesh = new THREE.Mesh(jointGeo, this.jointMat);
            this.joints.push(mesh);
            this.group.add(mesh);
        }
        this.eef = new THREE.Mesh(new THREE.SphereGeometry(0.021, 14, 12), this.eefMat);
        this.group.add(this.eef);

        const fingerGeo = new THREE.CylinderGeometry(0.006, 0.006, 0.055, 8);
        for (let i = 0; i < 2; i += 1) {
            const mesh = new THREE.Mesh(fingerGeo, this.eefMat);
            this.fingers.push(mesh);
            this.group.add(mesh);
        }

        // 基座：底板 + 立柱（只为示意安装位置）——**DH 基座系里 z 轴向上**（内容容器负责转 y-up）
        const baseMat = new THREE.MeshStandardMaterial({ color: palette.base, roughness: 0.8, transparent, opacity });
        const group = new THREE.Group();
        const plate = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.16, 0.012), baseMat);
        plate.position.set(0, 0, -0.006);
        const post = new THREE.Mesh(new THREE.CylinderGeometry(0.052, 0.052, 0.05, 20), baseMat);
        post.rotation.x = Math.PI / 2; // 圆柱默认沿 y：转到沿 z（底座立柱向上）
        post.position.set(0, 0, 0.025);
        group.add(plate, post);
        this.group.add(group);
    }

    /** 按关节值摆放连杆 / 关节球 / 夹爪。 */
    update(state: ArmState): void {
        const joints = fitJoints(state.joints);
        const chain = fkChain(joints);
        const points = chain.map((pose) => new THREE.Vector3(pose.p[0], pose.p[1], pose.p[2]));

        for (let i = 0; i < ARM_DOF; i += 1) {
            orientCylinder(this.links[i], points[i], points[i + 1]);
            this.joints[i].position.copy(points[i + 1]);
        }
        this.eef.position.copy(points[ARM_DOF]);

        // 夹爪：沿法兰 x 轴张开（0 = 闭合，1 = 张开），指向法兰 z 轴
        const last = chain[ARM_DOF];
        const xAxis = new THREE.Vector3(last.R[0], last.R[3], last.R[6]).normalize();
        const zAxis = new THREE.Vector3(last.R[2], last.R[5], last.R[8]).normalize();
        const open = Math.min(1, Math.max(0, state.gripper ?? 0));
        const spread = 0.008 + 0.024 * open;
        this.fingers.forEach((finger, i) => {
            const side = i === 0 ? 1 : -1;
            const from = points[ARM_DOF].clone().addScaledVector(xAxis, side * spread);
            const to = from.clone().addScaledVector(zAxis, 0.055);
            orientCylinder(finger, from, to);
        });
    }

    place(y: number): void {
        this.group.position.set(0, y, 0);
    }

    setColors(palette: ScenePalette, ghost: boolean): void {
        this.linkMat.color.setHex(ghost ? palette.ghost : palette.link);
        this.jointMat.color.setHex(ghost ? palette.ghost : palette.joint);
        this.eefMat.color.setHex(ghost ? palette.ghost : palette.eef);
    }

    dispose(): void {
        this.group.traverse((obj) => {
            if (obj instanceof THREE.Mesh) {
                obj.geometry.dispose();
            }
        });
        this.linkMat.dispose();
        this.jointMat.dispose();
        this.eefMat.dispose();
    }
}

/** 把圆柱摆到 `from → to`（位置 = 中点，朝向 = 轴对齐）。 */
function orientCylinder(mesh: THREE.Mesh, from: THREE.Vector3, to: THREE.Vector3): void {
    const dir = new THREE.Vector3().subVectors(to, from);
    const length = dir.length();
    if (length < 1e-6) {
        mesh.visible = false;
        return;
    }
    mesh.visible = true;
    mesh.position.copy(from).addScaledVector(dir, 0.5);
    mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.clone().normalize());
    mesh.scale.set(1, length, 1);
}

/**
 * 3D 视口场景：机械臂（连杆 + 关节点，**不加载 URDF**）+ 期望轨迹 / 实测轨迹折线。
 *
 * 姿态来源：`/v1/preview` 的 qpos（实测）/ 手动目标 / 推理动作块的关节段，全部经
 * kinematics 的同一份 DH 正解——网页画的姿态与真机姿态同源（近似显示，不做装配体）。
 */
export class ArmScene {
    private readonly scene = new THREE.Scene();
    private readonly camera: THREE.PerspectiveCamera;
    private readonly renderer: THREE.WebGLRenderer;
    private readonly controls: OrbitControls;
    /** z-up（DH 基座系）→ y-up（three 世界）的旋转容器 */
    private readonly content = new THREE.Group();
    private rigs: ArmRig[] = [];
    private ghostRigs: ArmRig[] = [];
    private trajectoryLines: THREE.Line[] = [];
    private trailLines: THREE.Line[] = [];
    private grid: THREE.GridHelper;
    private palette: ScenePalette;
    private frame = 0;
    private readonly observer: ResizeObserver;

    constructor(private readonly container: HTMLElement, theme: "light" | "dark") {
        this.palette = PALETTES[theme];
        this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        this.renderer.domElement.style.width = "100%";
        this.renderer.domElement.style.height = "100%";
        this.renderer.domElement.style.display = "block";
        container.appendChild(this.renderer.domElement);

        this.scene.add(new THREE.HemisphereLight(0xffffff, 0x666666, 1.5));
        const dir = new THREE.DirectionalLight(0xffffff, 1.1);
        dir.position.set(1, 2, 1.4);
        this.scene.add(dir);

        this.grid = new THREE.GridHelper(2.4, 24, this.palette.grid2, this.palette.grid);
        (this.grid.material as THREE.Material).transparent = true;
        (this.grid.material as THREE.Material).opacity = 0.75;
        this.scene.add(this.grid);

        // DH 基座系（z 向上）→ three.js 的 y-up（网格 / 相机都用 y-up）
        this.content.rotation.x = -Math.PI / 2;
        this.scene.add(this.content);

        this.camera = new THREE.PerspectiveCamera(42, 1, 0.02, 50);
        this.camera.position.copy(DEFAULT_CAMERA);
        this.camera.up.set(0, 1, 0);

        this.controls = new OrbitControls(this.camera, this.renderer.domElement);
        this.controls.enableDamping = true;
        this.controls.dampingFactor = 0.12;
        this.controls.target.copy(DEFAULT_TARGET);
        this.controls.minDistance = 0.25;
        this.controls.maxDistance = 4;
        this.controls.update();

        this.observer = new ResizeObserver(() => this.resize());
        this.observer.observe(container);
        this.resize();
        this.loop();
    }

    /** 主题切换：更新材质颜色与网格。 */
    setTheme(theme: "light" | "dark"): void {
        this.palette = PALETTES[theme];
        this.grid.material.dispose();
        this.scene.remove(this.grid);
        this.grid = new THREE.GridHelper(2.4, 24, this.palette.grid2, this.palette.grid);
        (this.grid.material as THREE.Material).transparent = true;
        (this.grid.material as THREE.Material).opacity = 0.75;
        this.scene.add(this.grid);
        this.rigs.forEach((rig) => rig.setColors(this.palette, false));
        this.ghostRigs.forEach((rig) => rig.setColors(this.palette, true));
    }

    /** 设置每臂姿态（`targets` 非空时同时画目标位姿的半透明「影子臂」，与实测臂同一基座位置）。 */
    setArms(arms: ArmState[], targets: ArmState[] | null): void {
        const count = arms.length;
        this.ensureRigs(count);
        this.rigs.forEach((rig, i) => {
            const used = i < count;
            rig.group.visible = used;
            this.ghostRigs[i].group.visible = used && targets?.[i] !== undefined;
            if (!used) return;
            const offset = this.offsetFor(i);
            rig.place(offset);
            // 影子臂必须与实测臂**同一基座位置**，否则两套底座不重合
            this.ghostRigs[i].place(offset);
            rig.update(arms[i]);
            if (targets?.[i]) this.ghostRigs[i].update(targets[i]);
        });
    }

    /** 期望轨迹折线（世界系点由对应臂的基座系给出，随基座偏移一起变换）。 */
    setPolylines(trajectories: Polyline[]): void {
        this.replaceLines(this.trajectoryLines, trajectories, false);
    }

    /** 复位视角（用户拖歪了可以一键回到默认机位）。 */
    resetView(): void {
        this.camera.position.copy(DEFAULT_CAMERA);
        this.controls.target.copy(DEFAULT_TARGET);
        this.controls.update();
    }

    dispose(): void {
        cancelAnimationFrame(this.frame);
        this.observer.disconnect();
        this.controls.dispose();
        this.rigs.forEach((rig) => rig.dispose());
        this.ghostRigs.forEach((rig) => rig.dispose());
        [...this.trajectoryLines, ...this.trailLines].forEach((line) => {
            line.geometry.dispose();
            (line.material as THREE.Material).dispose();
        });
        this.renderer.dispose();
        if (this.renderer.domElement.parentElement === this.container) {
            this.container.removeChild(this.renderer.domElement);
        }
    }

    // ---- 内部 ---------------------------------------------------------------
    /** 双臂安装位（世界 y）：左臂（序号 0）在原点，右臂偏 -0.7 m；朝向同为 +x（单臂 = 原点）。 */
    private offsetFor(index: number): number {
        return -index * ARM_SEPARATION;
    }

    private ensureRigs(count: number): void {
        const wanted = Math.max(1, count);
        while (this.rigs.length < wanted) {
            const solid = new ArmRig(1, this.palette, false);
            const ghost = new ArmRig(0.32, this.palette, true);
            ghost.setColors(this.palette, true);
            this.rigs.push(solid);
            this.ghostRigs.push(ghost);
            this.content.add(solid.group, ghost.group);
        }
    }

    private replaceLines(target: THREE.Line[], polylines: Polyline[], dashed: boolean): void {
        // 折线点数少、刷新频率低（≤ 1Hz / 每次推理一次）：直接重建比做环形缓冲更简单可靠
        target.forEach((line) => {
            line.removeFromParent();
            line.geometry.dispose();
            (line.material as THREE.Material).dispose();
        });
        target.length = 0;
        polylines.forEach((polyline) => {
            if (polyline.points.length < 2) return;
            const geometry = new THREE.BufferGeometry().setFromPoints(
                polyline.points.map((p) => new THREE.Vector3(p[0], p[1], p[2]))
            );
            const material = new THREE.LineBasicMaterial({
                color: dashed ? this.palette.trail : polyline.color,
                transparent: true,
                opacity: dashed ? 0.75 : 1,
                linewidth: 2,
            });
            const line = new THREE.Line(geometry, material);
            const rig = this.rigs[polyline.armIndex]?.group ?? this.content;
            rig.add(line);
            target.push(line);
        });
    }

    private resize(): void {
        const width = this.container.clientWidth;
        const height = this.container.clientHeight;
        if (width === 0 || height === 0) return;
        this.renderer.setSize(width, height, false);
        this.camera.aspect = width / height;
        this.camera.updateProjectionMatrix();
    }

    private loop = (): void => {
        this.frame = requestAnimationFrame(this.loop);
        if (document.hidden) return;
        this.controls.update();
        this.renderer.render(this.scene, this.camera);
    };
}
