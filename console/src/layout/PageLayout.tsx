import type { ReactNode } from "react";

/**
 * 页面三栏布局：**左侧操作栏 / 中间主视口 / 右侧相机栏**（图纸里的固定分工）。
 *
 * - 左栏：当前页面独有的控制与参数（窄、可滚动）；
 * - 中栏：3D 视口 + 关键数值（占满剩余宽度）；
 * - 右栏：摄像头视频流 + 页面附加信息。
 *
 * **始终三列**（窄屏只压缩两翼：左 15rem / 右 16rem，中栏 `minmax(0,1fr)` 自适应；
 * `xl` 以上放松到 19rem / 21rem）——三列分工是这套界面的骨架，塌成一列会让 3D 与相机
 * 掉到左栏下面看不见。
 */
export function PageLayout({ left, center, right }: { left: ReactNode; center: ReactNode; right: ReactNode }) {
    return (
        <div className="grid min-h-0 flex-1 gap-3 overflow-y-auto p-3 grid-cols-[15rem_minmax(0,1fr)_16rem] xl:grid-cols-[19rem_minmax(0,1fr)_21rem] xl:overflow-hidden">
            <aside className="min-w-0 space-y-2 xl:overflow-y-auto xl:pr-1">{left}</aside>
            <section className="flex min-h-0 min-w-0 flex-col gap-3">{center}</section>
            <aside className="min-w-0 space-y-2 xl:overflow-y-auto xl:pr-1">{right}</aside>
        </div>
    );
}
