# 上传会话（UploadSession）

## 摘要

`UploadSession` 管理 Edge 本地采集目录中的 episode 文件：扫描并配对同名的 `.mcap` 与 `.json`，读取 JSON 元数据，生成可校验的 episode 汇总，支持按 episode 编号选择并维护上传状态。

## 目标与边界

-   支持配置默认目录，并允许 HTTP 创建请求覆盖目录。
-   以文件名 stem 作为 episode 标识，例如 `episode_0.mcap` 与 `episode_0.json` 配成一个 episode。
-   JSON 元数据暂不限制字段；Edge 保留原始字段，并补充文件路径、大小、修改时间与 SHA-256。
-   缺少配对文件、JSON 无法解析或 JSON 顶层不是对象时，该 episode 进入 `invalid`，扫描仍继续。
-   选择按 episode 标识处理，不按目录文件行号处理。
-   当前阶段只实现本地扫描、汇总、选择和上传队列状态；远端上传目标未配置时，上传动作返回 `501`，不删除本地源文件。
-   UploadSession 不进入 EdgeNode 的机器人任务状态机，不占用 RobotAdapter；它是 server 层管理的文件会话。

## Episode 汇总

每个 episode 返回：

-   `episode_id`：文件 stem，例如 `episode_0`。
-   `status`：`ready`、`invalid`、`pending`、`uploading`、`succeeded` 或 `failed`。
-   `mcap` / `metadata`：文件存在性、绝对路径、大小、修改时间、SHA-256。
-   `metadata_content`：JSON 原始对象；解析失败时为 `null`。
-   `errors`：配对或解析错误列表。

扫描结果还返回 `folder_path`、扫描时间和 episode 数量。默认不读取 `.mcap` 内容，只读取文件元信息并计算 checksum。

## HTTP 控制面

-   `POST /v1/uploads`：创建或重扫 UploadSession；请求可选 `folder_path`，缺省使用 `upload.data_dir`。
-   `GET /v1/uploads`：获取当前扫描汇总。
-   `POST /v1/uploads/select`：按 `episode_ids` 替换选择集，只允许选择 `ready` / 已失败可重试的 episode。
-   `POST /v1/uploads/upload`：将当前选择加入上传队列；没有配置上传目标时返回 `501`。
-   `POST /v1/uploads/retry`：重置选择集中 `failed` episode 为 `pending`；没有上传目标时仍不执行网络传输。

UploadSession 使用线程锁保护当前汇总和选择集；源文件只读，上传成功不自动删除。

## 配置

```yaml
upload:
  data_dir: /path/to/data
  endpoint: null # 远端上传服务；未配置时上传接口返回 501
```
