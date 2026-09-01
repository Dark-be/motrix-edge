# 采集元信息选项（capture meta）实施计划

## 摘要

-   `capture.yml`（包内默认 / `MOTRIX_CONFIG_DIR` 外界目录）维护可拓展的「元信息分类 → 选项数组」；`capture meta` 命令族
    （list / add / edit / delete / delete-key）创建 / 编辑 / 删除；`GET /v1/captures/meta`
    暴露选项供前端选择列表；`capture sync` 不变，只负责把选中元信息同步到机器人进程。

## TODO

-   [x] 设计文档：`wiki/design/motrix_edge_capture_meta.md`
-   [x] `capture.yml`：`meta` 段（operator / task_name 示例数组；包内默认）
-   [x] `utils/capture_meta.py`：`CaptureMetaStore`（load / save / list / add / edit /
        delete / delete-key；RLock；缺省可写路径：外界目录 / 数据目录）
-   [x] `utils/commands.py`：注册 `capture meta list/add/edit/delete/delete-key` +
        `handle_capture_meta` 处理器（配置级，缺省默认 store）
-   [x] `node.py`：`_dispatch` 任何状态响应 `capture meta`（注入 `capture_meta_store`）
-   [x] `session/base.py`：`_on_capture_meta`；CaptureSession / InferSession 任务态响应
-   [x] `server/capture.py` + `server/app.py`：`GET /v1/captures/meta`
-   [x] 前端：`SessionPanel` 采集人员 / 任务改为选择列表（读 `/v1/captures/meta`）
-   [x] 单元测试（store CRUD / handle_capture_meta / node 分发 / server 端点）+ 容器内校验
