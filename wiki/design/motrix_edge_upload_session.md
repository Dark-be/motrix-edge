# 上传会话（UploadSession）

## 摘要

`UploadSession` 管理 Edge 本地采集目录中的 episode 文件：扫描并配对同名的 `.mcap` 与 `.json`，**schema 驱动解析 JSON 描述文件**为结构化元信息，生成可校验的 episode 汇总，支持按 episode 编号选择、**打包**，并维护上传队列状态。

> **本版本范围**：只做**本地文件的查看 / 筛选 / 打包**。上传 API（`/v1/uploads/upload`、`/v1/uploads/retry`）是**为数据平台预留的**——程序化上传在后续版本加入；本版本里数采人员在前端确认打包结果后，**手动上传**文件到数据平台。

## 目标与边界

-   支持配置默认目录，并允许 HTTP 创建请求覆盖目录；缺省目录回退链为：请求 `folder_path` → **adapter 数据目录**（`node.capture_status.data_dir`，与 `GET /v1/captures` 同源）→ `upload.data_dir` 配置。
-   以文件名 stem 作为 episode 标识，例如 `episode_0.mcap` 与 `episode_0.json` 配成一个 episode。
-   JSON 描述文件按 **schema 单点定义**提取为结构化 `meta` 字段（见下节）；未知字段保留在 `metadata_unknown`，原始 JSON 保留在 `metadata_content`（向前兼容）。
-   缺少配对文件、JSON 无法解析或 JSON 顶层不是对象时，该 episode 进入 `invalid`，扫描仍继续。
-   选择按 episode 标识处理，不按目录文件行号处理。
-   当前阶段只实现本地扫描、汇总、选择、打包与上传队列状态；上传目标（`upload.endpoint`，面向数据平台）未配置时，上传动作返回 `501`，不删除本地源文件。
-   UploadSession 不进入 EdgeNode 的机器人任务状态机，不占用 RobotAdapter；它是 server 层管理的文件会话。

## 元信息解析（schema 驱动）

-   JSON 描述文件（`episode_<n>.json`）的已知字段由 **schema** 单点定义：`字段名 → (类型, 描述)`；新增字段只需在 schema 加一行，解析与前端展示自动跟随。
-   当前 schema 覆盖：`relative_path` / `robot_name` / `robot_type` / `operator` / `task_name` / `frames` / `size_bytes` / `duration` / `sha256` / `created_at`。
-   **字段名的权威在写侧**：这些字段由 robot-pipeline 的 `ActMcapCollector`（`finish()` 写同名 `.json`）
    产出；本 schema 只是读取侧的投影——改名 / 新增字段时**两侧一起改**（本文件与 collector 的
    `_write_meta_json`），未识别的字段会落到 `metadata_unknown` 不会丢。
-   类型归一化：`frames` / `size_bytes` → `int`，`duration` → `float`，其余 → `str`；缺省字段可空（不判 invalid），仅 JSON 损坏 / 顶层非对象 / 配对缺失才 `invalid`。
-   解析产物三份：
    -   `meta`：结构化已知字段（前端展示用）。
    -   `metadata_unknown`：schema 未识别的原始字段（向前兼容新数据）。
    -   `metadata_content`：完整原始 JSON。

## Episode 汇总

每个 episode 返回：

-   `episode_id`：文件 stem，例如 `episode_0`。
-   `status`：`ready`、`invalid`、`pending`、`uploading` 或 `succeeded` / `failed`——**本版本只有前两个
    可达**（未配置 `upload.endpoint` 时 `enqueue` / `retry` 直接 `501`），后几个为后续 uploader 预留。
-   `mcap` / `metadata`：文件存在性、绝对路径、大小、修改时间、SHA-256。
-   `meta`：JSON 描述文件的结构化字段（schema 提取，缺省字段为 `null`）。
-   `metadata_content`：JSON 原始对象；解析失败时为 `null`。
-   `metadata_unknown`：schema 未识别的原始字段。
-   `errors`：配对或解析错误列表。

扫描结果还返回 `folder_path`、扫描时间和 episode 数量。默认不读取 `.mcap` 内容，只读取文件元信息并计算 checksum。

## HTTP 控制面

**受控操作**：所有 `/v1/uploads/*` 端点都要带有效租约（`X-Lease-Id`，与采集控制面同一规则）——
打包会移动数据文件、扫描会读取目录内容，都不允许匿名调用（缺失租约 `409` / 租约不匹配 `403`）。

**扫描目录白名单**：`folder_path` 只允许落在**数据目录**（adapter 上报的采集目录 / `upload.data_dir`）
及其子目录内；越界 → `400`，两个来源都没有（未配 `upload.data_dir` 且未绑定进程）→ `409`
（没配置就不默认放开任意路径）。

**重操作互斥**：`scan` 与 `pack` 同一时刻只允许一个在跑（都要对整目录算 SHA-256 / 搬运文件），
并发触发 → `409`（`already in progress`），避免把控制面拖住。

-   `POST /v1/uploads`：创建或重扫 UploadSession；请求可选 `folder_path`，缺省回退链为 adapter 数据目录 → `upload.data_dir`。
-   `GET /v1/uploads`：获取当前扫描汇总。
-   `POST /v1/uploads/select`：按 `episode_ids` 替换选择集；可选中状态 = `ready` / `pending` / `failed`
    （`invalid` 与 `uploading` / `succeeded` 不可选）。
-   `POST /v1/uploads/upload`：将当前选择加入上传队列；没有配置上传目标时返回 `501`。
-   `POST /v1/uploads/retry`：重置选择集中 `failed` episode 为 `pending`；没有上传目标时仍不执行网络传输。
-   `POST /v1/uploads/pack`：把当前选择**打包**（**移动**）到 `<folder_path>/<包名>/`；请求可选
    `name`（缺省 `pack<选中数量>`）；重名 `409`、非法名 `400`、无扫描 / 无选择 `409`、并发中 `409`；见下节。
    收尾重扫失败**不影响打包成功**（仍 `200`，见「回执」的 `warnings` / `scan=null`）。

`GET /v1/uploads` 的汇总额外返回 `suggested_pack_name`（`pack<选中数量>`；未扫描 / 未选择时为空）与
`endpoint_configured`（是否配了上传目标），供前端预填包名 / 决定上传按钮可用性。

UploadSession 用 `RLock` 保护汇总 / 选择集，另有一把重操作锁让 `scan` / `pack` 互斥；
除 `pack` 外不改动源文件（上传成功不自动删除）。

**锁粒度**：`pack` 的重 IO（建目录 + 搬文件）在**锁外**进行——锁只包住「校验 + 快照搬运计划」
与「清空选择集」两小段。否则 GB 级数据搬运期间，前端的 `GET /v1/uploads` 状态轮询会被阻塞
到打包结束（与 `scan` 一致：扫描算哈希时也不持锁）。

## 打包（pack）

**用途**：本版本**不做网络传输**——把选中的数据整理成**可整体搬运的包目录**，由数采人员把这个
包**手动上传**到数据平台（也便于人工拷贝 / 归档 / 交接）。

**规则**：

-   包目录建在**当前扫描目录**下：`<folder_path>/<包名>/`。
-   包名默认 `pack<选中数量>`（如选中 10 个 → `pack10`）；调用方（前端）可改名。
    -   **重名即拒绝**（`409`）：不覆盖、不合并——需要另起一个名字（默认名已存在时前端提示改名）。
    -   包名必须是**单个安全路径段**：非空、不含路径分隔符、不是 `.` / `..`、不以 `.` 开头
        （否则会逃出扫描目录或生成隐藏目录）→ 非法名 `400`。
-   内容 = 选中 episode 的 `.mcap` + `.json`，**移动**（不是复制）：打包后源目录不再保留这两个文件。
    与「上传」不同（上传成功不删源文件），打包是**本地整理**，会改变数据位置。
-   扫描是**非递归**的（只扫一层），所以包目录内的文件不会被再次扫到；打包后重扫，
    这些 episode 从列表消失、选择集清空。
-   收尾重扫是**尽力而为**：走到这一步时文件已全部移动、选择集已清空，**打包已经成功**；
    重扫若失败（目录被删 → `404`、白名单变化 → `409`），降级为 `scan=null` + `warnings`，
    **不把成功的打包报成失败**。
-   失败**回滚**：任一文件移动失败 → 已移动的移回原处、删除刚建的空目录，返回 `500`（不留半成品包）；
    **回滚绝不删数据**——移回失败的文件保留在包目录里，错误信息给出路径（宁可留下残留也不静默删除）。
-   前置校验：必须已扫描（`folder_path` 已知）且有选择集；源文件必须存在（缺失 → `404`）。

**回执**：`{name, path, episode_count, file_count, files[], episode_ids[], scan, warnings[]}`

-   `scan`：打包后的重扫状态（前端直接替换列表；源文件已移入包目录，这些 episode 不再出现）；
    **重扫失败时为 `null`**——此时**不能用它替换列表**（会清空界面），应据 `warnings` 提示
    「请重新扫描」并调 `POST /v1/uploads` 重新取状态；
-   `warnings[]`：**非致命**提示，当前只有「收尾重扫失败」一种；为空表示一切正常。
    **打包成功 = `200`**，`warnings` 非空不代表失败（前端按提示做，不要当错误弹）。

## 配置

```yaml
upload:
    data_dir: null # 缺省扫描目录（null = 运行时回退 adapter 数据目录）
    endpoint: null # 数据平台上传服务地址（预留）；未配置时上传接口返回 501，不删除源文件
```

## 后续版本（当前未实现）

-   **程序化上传**：`upload.endpoint` 指向数据平台的上传 API，由 Edge 直接把包 / episode 传上去
    （含进度、重试与失败可见性）。当前未配置时 `POST /v1/uploads/upload` / `retry` 返回 `501`，
    且**不动源文件**——本版本由数采人员把包目录手动上传到数据平台。
-   **前端（随 edge-console 落地，不在本仓库）**：episode 卡片化展示结构化元信息、包名输入框
    （预填 `suggested_pack_name`）与「打包选中」按钮、上传面板。
-   **边界（有意不做）**：不再递归扫描子目录；**不做压缩**（`.tar` / `.zip`）——包就是一个目录，
    便于人工拷贝、归档，也便于后续上传实现就地读取；上传成功后是否删源文件由后续版本定义
    （打包是**本地整理**，会移动源文件；上传不删源文件）。
