# 推理策略选择实施计划

## 摘要

进入推理会话前必须显式选择已注册策略；HTTP、命令总线与 Web Console 使用同一 `policy_type` 契约。

## TODO

-   [ ] 后端要求 `POST /v1/infers` 显式提交 `policy_type`，并校验注册表。
-   [ ] 命令总线启动推理会话时要求并透传 `policy_type`。
-   [ ] 推理状态返回当前会话实际选择的策略类型与连接成功后的服务端 metadata。
-   [ ] Web Console 展示已注册策略下拉框，未选择时禁止进入推理，并展示服务端 metadata。
-   [ ] 补充后端测试并在 Docker 中运行 Python 测试；前端在宿主机外部终端构建。
