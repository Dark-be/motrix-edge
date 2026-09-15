# UploadSession 实施计划

## 摘要

实现本地采集目录扫描、episode 配对、**schema 驱动的 JSON 描述文件结构化解析**、前端可读展示、选择性队列和上传状态查询；当前不配置远端上传目标时，上传接口返回 `501`，不删除源文件。

## TODO

-   [x] 增加 UploadSession：扫描 `.mcap` / `.json`、解析元数据、生成 checksum 和状态。
-   [x] 增加 `/v1/uploads` HTTP 接口：创建/重扫、查询、选择、上传占位和失败重试。
-   [x] 增加 `upload.data_dir` 配置和前端 UploadSession 面板。
-   [x] 补充扫描、配对、选择和状态接口测试。
-   [x] 运行 Docker 后端测试和宿主机前端构建。
-   [x] 增加 schema 驱动的 JSON 描述文件结构化解析（`meta` / `metadata_unknown`，类型归一化）。
-   [x] 扫描缺省目录回退链：请求 `folder_path` → adapter 数据目录 → `upload.data_dir`。
-   [x] 前端 episode 卡片化展示结构化元信息（robot / operator / task_name / frames / duration / 大小 / created_at）。
-   [x] 前端上传面板「获取数据目录」按钮（`GET /v1/captures`）把 adapter 数据目录填入目录框（仅按钮触发，无自动填充）；修复 `/v1/captures` 读取 `data_dir` 字段（原误读 `save_dir`）。
-   [x] 补充元信息解析与缺省目录测试；运行后端测试 + 前端构建。
-   [x] 前端 Episode 列表大规模优化：搜索 / 状态筛选 / 排序 / 分页（每页 50）/ 批量全选，只渲染当前页。
