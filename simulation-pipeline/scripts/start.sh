#!/bin/bash
# 启动 MotrixSim 仿真机器人进程（Edge simulation_piper 承载端）。
#
# 用法:
#   bash scripts/start.sh
#   bash scripts/start.sh --scene assets/scenes/press_three_buttons/scene.json \
#       --session tasks/press_three_buttons/session.toml
#   bash scripts/start.sh --help
#
# 环境变量（命令行同名参数可覆盖）：
#   MOTRIX_SIMULATION_WORKSPACE  MotrixGen 任务工作区
#   MOTRIX_SIMULATION_SESSION    相对工作区的 session TOML
#   MOTRIX_SIMULATION_SCENE      相对工作区的 scene JSON
#   MOTRIX_SIMULATION_OUTPUT     SDK 诊断输出目录（必须尚不存在）

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ $# -gt 0 ] && { [ "$1" = "--help" ] || [ "$1" = "-h" ]; }; then
    exec uv run --locked motrix-simulation-pipeline "$@"
fi

WORKSPACE="${MOTRIX_SIMULATION_WORKSPACE:-/home/pc16/project/manipulation-tasks}"
SESSION="${MOTRIX_SIMULATION_SESSION:-tasks/stack_color_blocks/session.toml}"
SCENE="${MOTRIX_SIMULATION_SCENE:-assets/scenes/stack_color_blocks/scene.json}"

if [ -n "${MOTRIX_SIMULATION_OUTPUT:-}" ]; then
    OUTPUT="$MOTRIX_SIMULATION_OUTPUT"
else
    OUTPUT="$(mktemp -d /tmp/motrix-simulation-pipeline.XXXXXX)"
    rmdir "$OUTPUT"
fi

if [ ! -d "$WORKSPACE" ]; then
    echo "错误：MotrixGen 工作区不存在：$WORKSPACE" >&2
    exit 1
fi
if [ ! -f "$WORKSPACE/$SESSION" ]; then
    echo "错误：session 不存在：$WORKSPACE/$SESSION" >&2
    exit 1
fi
if [ ! -f "$WORKSPACE/$SCENE" ]; then
    echo "错误：scene 不存在：$WORKSPACE/$SCENE" >&2
    exit 1
fi
if [ -e "$OUTPUT" ]; then
    echo "错误：output 必须尚不存在：$OUTPUT" >&2
    exit 1
fi

echo "==> 启动 MotrixSim 仿真机器人"
echo "==> workspace: $WORKSPACE"
echo "==> session:   $SESSION"
echo "==> scene:     $SCENE"
echo "==> output:    $OUTPUT"
echo "==> endpoint:  http://127.0.0.1:8090（可用 --host / --port 覆盖）"
echo "==> 命令: uv run --locked motrix-simulation-pipeline --workspace $WORKSPACE --session $SESSION --scene $SCENE --output $OUTPUT $*"

exec uv run --locked motrix-simulation-pipeline \
    --workspace "$WORKSPACE" \
    --session "$SESSION" \
    --scene "$SCENE" \
    --output "$OUTPUT" \
    "$@"
