# Piper robot assets

## 摘要

本目录保存从 AgileX 最新官方 `agx_arm_urdf` 转换、并与官方 `piper_sdk` 新固件关节 frame 核对的
Piper 六轴 MJCF，供 MotrixSim 运动学和动力学使用。真机控制仍以 SDK 可接受的软限位为准，不能
仅凭模型的机械范围直接扩大硬件下发边界。

## 模型入口

-   `mjcf/arm/piper.xml`：不含夹爪的六轴模型；资源路径相对于该文件解析。
-   `mjcf/arm/resources/`：模型引用的 STL mesh，必须与 XML 一起分发。
-   `mjcf/arm/LICENSE`、`mjcf/arm/NOTICES`：上游许可证及来源声明。

## 官方来源与固定版本

-   URDF 与 mesh：<https://github.com/agilexrobotics/agx_arm_urdf>
-   固定版本：`680fa925cd00db7e2628563d2edc5540963045de`
-   官方入口：`piper/urdf/piper_description.urdf`
-   DH 定义：<https://github.com/agilexrobotics/piper_sdk/blob/master/piper_sdk/kinematics/piper_fk.py>
-   新固件默认值提交：`c12cc14c9bc4983879c11aabfe5b0bbfcd7a0f1f`

## 本地校准

官方 URDF 的关节 frame、质量、质心、完整惯量张量、机械限位和 STL collision mesh 被转换进
MJCF；joint2/joint3 固定偏移分别为新固件的 `-172.22°` 和 `-102.78°`。URDF 中 link6 的
`0.088259 mm` X 偏移被保留，因此其末端位置与忽略该 CAD 微偏移的 SDK MDH 最多相差约 0.1 mm。
MJCF position actuator 的控制范围与 URDF 机械限位保持一致。

真机下发限位独立处理：J1–J5 的 SDK preset 与 URDF 一致；J6 最新 URDF 是 ±170°，但 SDK 弧度
preset 是 ±120°，所以控制器采用 ±120° 并继续内缩浮点安全余量。

更新模型时必须重新锁定官方版本、保留许可证和声明，并运行模型资产完整性测试、官方 URDF 帧
数值一致性测试及真机 FK 对照验证。
