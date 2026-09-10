# 上传会话（UploadSession）

## 摘要

`UploadSession` 管理 Edge 本地采集目录中的 episode 文件：扫描并配对同名的 `.mcap` 与 `.json`，**schema 驱动解析 JSON 描述文件**为结构化元信息，生成可校验的 episode 汇总，支持按 episode 编号选择并维护上传状态。

## 目标与边界

-   支持配置默认目录，并允许 HTTP 创建请求覆盖目录；缺省目录回退链为：请求 `folder_path` → **adapter 数据目录**（`node.data_status.data_dir`）→ `upload.data_dir` 配置。
-   以文件名 stem 作为 episode 标识，例如 `episode_0.mcap` 与 `episode_0.json` 配成一个 episode。
-   JSON 描述文件按 **schema 单点定义**提取为结构化 `meta` 字段（见下节）；未知字段保留在 `metadata_unknown`，原始 JSON 保留在 `metadata_content`（向前兼容）。
-   缺少配对文件、JSON 无法解析或 JSON 顶层不是对象时，该 episode 进入 `invalid`，扫描仍继续。
-   选择按 episode 标识处理，不按目录文件行号处理。
-   当前阶段只实现本地扫描、汇总、选择和上传队列状态；远端上传目标未配置时，上传动作返回 `501`，不删除本地源文件。
-   UploadSession 不进入 EdgeNode 的机器人任务状态机，不占用 RobotAdapter；它是 server 层管理的文件会话。

## 元信息解析（schema 驱动）

-   JSON 描述文件（`episode_<n>.json`）的已知字段由 **schema** 单点定义：`字段名 → (类型, 描述)`；新增字段只需在 schema 加一行，解析与前端展示自动跟随。
-   当前 schema 覆盖：`relative_path` / `robot_name` / `robot_type` / `operator` / `task_name` / `frames` / `size_bytes` / `duration` / `sha256` / `created_at`。
-   类型归一化：`frames` / `size_bytes` → `int`，`duration` → `float`，其余 → `str`；缺省字段可空（不判 invalid），仅 JSON 损坏 / 顶层非对象 / 配对缺失才 `invalid`。
-   解析产物三份：
    -   `meta`：结构化已知字段（前端展示用）。
    -   `metadata_unknown`：schema 未识别的原始字段（向前兼容新数据）。
    -   `metadata_content`：完整原始 JSON。

## Episode 汇总

每个 episode 返回：

-   `episode_id`：文件 stem，例如 `episode_0`。
-   `status`：`ready`、`invalid`、`pending`、`uploading`、`succeeded` 或 `failed`。
-   `mcap` / `metadata`：文件存在性、绝对路径、大小、修改时间、SHA-256。
-   `meta`：JSON 描述文件的结构化字段（schema 提取，缺省字段为 `null`）。
-   `metadata_content`：JSON 原始对象；解析失败时为 `null`。
-   `metadata_unknown`：schema 未识别的原始字段。
-   `errors`：配对或解析错误列表。

扫描结果还返回 `folder_path`、扫描时间和 episode 数量。默认不读取 `.mcap` 内容，只读取文件元信息并计算 checksum。

## HTTP 控制面

-   `POST /v1/uploads`：创建或重扫 UploadSession；请求可选 `folder_path`，缺省回退链为 adapter 数据目录 → `upload.data_dir`。
-   `GET /v1/uploads`：获取当前扫描汇总。
-   `POST /v1/uploads/select`：按 `episode_ids` 替换选择集，只允许选择 `ready` / 已失败可重试的 episode。
-   `POST /v1/uploads/upload`：将当前选择加入上传队列；没有配置上传目标时返回 `501`。
-   `POST /v1/uploads/retry`：重置选择集中 `failed` episode 为 `pending`；没有上传目标时仍不执行网络传输。
-   `POST /v1/uploads/pack`：把当前选择**打包**（移动）到 `<folder_path>/<包名>/`；请求可选
    `name`（缺省 `pack<选中数量>`）；重名 `409`、非法名 `400`、无扫描 / 无选择 `409`；见下节。

`GET /v1/uploads` 的汇总额外返回 `suggested_pack_name`（`pack<选中数量>`；未扫描 / 未选择时为空），
供前端预填包名。

UploadSession 使用线程锁保护当前汇总和选择集；源文件只读，上传成功不自动删除。

## 打包（pack）

**用途**：远端上传接口尚未接入时，先把选中的数据整理成**可整体搬运的包目录**（人工拷贝 / 后续上传
只需处理一个文件夹）。

**规则**：

-   包目录建在**当前扫描目录**下：`<folder_path>/<包名>/`。
-   包名默认 `pack<选中数量>`（如选中 10 个 → `pack10`）；调用方（前端）可改名。
    -   **重名即拒绝**（`409`）：不覆盖、不合并——需要另起一个名字（默认名已存在时前端提示改名）。
    -   包名必须是**单个安全路径段**：非空、不含路径分隔符、不是 `.` / `..`、不以 `.` 开头
        （否则会逃出扫描目录或生成隐藏目录）→ 非法名 `400`。
-   内容 = 选中 episode 的 `.mcap` + `.json`，**移动**（不是复制）：打包后源目录不再保留这两个文件。
    与「上传」不同（上传成功不删源文件），打包是**本地整理**，会改变数据位置。
-   扫描是**非递归**的（只扫一层），所以包目录内的文件不会被再次扫到；打包后自动重扫，
    这些 episode 从列表消失、选择集清空。
-   失败**回滚**：任一文件移动失败 → 已移动的移回原处、删除刚建的空目录，返回 `500`（不留下半成品包）。
-   前置校验：必须已扫描（`folder_path` 已知）且有选择集；源文件必须存在（缺失 → `404`）。

**回执**：`{name, path, episode_count, file_count, files[], episode_ids[], skipped[], scan}`——
`scan` 为打包后的重扫状态（前端直接替换列表）。

## 配置

```yaml
upload:
    data_dir: /path/to/data
    endpoint: null # 远端上传服务；未配置时上传接口返回 501
```
