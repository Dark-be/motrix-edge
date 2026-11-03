#!/bin/bash
# 从仓库根目录加载本机环境变量并启动 Motrix Edge。
#
# 用法：
#   bash scripts/start.sh
#   bash scripts/start.sh --config /etc/motrix-edge/edge.yml
#   bash scripts/start.sh --help
#
# 可选环境变量：
#   MOTRIX_EDGE_ENV_FILE  env 文件路径；未设置时使用仓库根目录的 .env。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${MOTRIX_EDGE_ENV_FILE:-$ROOT_DIR/.env}"
UV_ARGS=()
if [ -f "$ENV_FILE" ]; then
    UV_ARGS+=(--env-file "$ENV_FILE")
elif [ -n "${MOTRIX_EDGE_ENV_FILE:-}" ]; then
    echo "错误：MOTRIX_EDGE_ENV_FILE 指定的文件不存在：$ENV_FILE" >&2
    exit 1
else
    echo "==> 未找到 $ENV_FILE；不加载 env 文件" >&2
fi

echo "==> 启动 Motrix Edge"
if [ "${#UV_ARGS[@]}" -gt 0 ]; then
    echo "==> env: $ENV_FILE"
fi
echo "==> 命令: uv run ${UV_ARGS[*]} motrix-edge run $*"

exec uv run "${UV_ARGS[@]}" motrix-edge run "$@"
