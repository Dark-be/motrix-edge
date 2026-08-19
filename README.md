# motrix-edge

机器人边缘节点（`motrix_edge`）：对现有机器人做统一的二次抽象，作为边缘节点提供
接口给数据采集、推理与真机强化学习使用——边缘节点负责收集 observation 并**主动请
求**推理节点（Endpoint）返回 action，本地校验后限速执行。

> **状态**：当前为脚手架 + 文档阶段（冻结设计 / 计划契约）。任务运行时核心与 HTTP
> 控制面分别由后续 MR 落地，详见 [wiki/design](wiki/design/index.md) 与
> [wiki/plan](wiki/plan/index.md)。

## 快速开始

```bash
uv sync           # 安装依赖（含 dev 依赖）
uv run pytest     # 运行测试
uv run ruff check .
npm run format    # ruff format + prettier
```

## 目录结构

```
src/motrix_edge/     # 主包（当前为最小包骨架，完整结构随任务运行时 / 控制面 MR 落地）
config/edge.yml      # 边缘节点配置
wiki/design/         # 设计文档（架构 / 契约）
wiki/plan/           # 实施计划
tests/               # 测试
```

## 文档

-   架构与设计：[wiki/design/index.md](wiki/design/index.md)
-   实施计划：[wiki/plan/index.md](wiki/plan/index.md)
-   仓库约定：[CLAUDE.md](CLAUDE.md)

## 开发

提交前依次执行：`npm run format` → `uv run ruff check .` → `uv run pytest`。
