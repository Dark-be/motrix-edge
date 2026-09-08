# UploadSession 实施计划

## 摘要

实现本地采集目录扫描、episode 配对、JSON 元数据汇总、选择性队列和上传状态查询；当前不配置远端上传目标时，上传接口返回 `501`，不删除源文件。

## TODO

- [x] 增加 UploadSession：扫描 `.mcap` / `.json`、解析元数据、生成 checksum 和状态。
- [x] 增加 `/v1/uploads` HTTP 接口：创建/重扫、查询、选择、上传占位和失败重试。
- [x] 增加 `upload.data_dir` 配置和前端 UploadSession 面板。
- [x] 补充扫描、配对、选择和状态接口测试。
- [x] 运行 Docker 后端测试和宿主机前端构建。
