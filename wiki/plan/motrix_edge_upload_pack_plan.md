# 上传会话打包（pack）实施计划

对应设计：[上传会话（UploadSession）](../design/motrix_edge_upload_session.md) 的「打包（pack）」节。

## 目标

远端上传接口未接入时，提供**打包**能力：把选中的 episode（`.mcap` + `.json`）**移动**到
`<当前扫描目录>/<包名>/`，包名默认 `pack<选中数量>`、可改名；重名拒绝；打包后自动重扫并清空选择集。

## TODO

-   [ ] `UploadSession.pack(name=None)`：包名校验（单段安全目录名）、重名 409、源文件缺失 404、
        **失败回滚**（已移动的移回 + 删除空目录 → 500）、成功后重扫 + 清空选择集
-   [ ] `UploadSession.status()` 增加 `suggested_pack_name`（`pack<选中数量>`）
-   [ ] server：`POST /v1/uploads/pack`（`{name?}`）+ 路由文档，`UploadPackRequest` 请求模型
-   [ ] 前端：`api.packUploads(name?)`、`UploadStatus.suggested_pack_name`、`UploadPackResponse` 类型
-   [ ] 前端 UploadPanel：包名输入（预填建议名）+「打包选中」按钮（无选择 / 无包名时禁用）+ 结果展示
-   [ ] 测试：默认包名与移动、重名 409、非法名 400、无选择 409、打包后重扫（episode 消失）、回滚；
        server 端点用例
-   [ ] wiki：设计文档「打包（pack）」节 + 控制面条目（本计划落地后删除本文件）

## 边界

-   不实现远端上传（`endpoint` 仍为 null → `POST /v1/uploads/upload` 保持 501）。
-   不递归扫描子目录：打包目录内的文件不会再次出现在列表里。
-   不做压缩（`.tar`/`.zip`）：包是一个目录，便于人工拷贝与后续上传实现就地读取。
