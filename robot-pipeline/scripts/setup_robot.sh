#!/bin/bash
# 机器档案一键生成 / 更新：**交互式逐个插设备**，自动填 RealSense 序列号 / V4L2 与串口稳定软链 /
# CAN 的 USB 物理口（bus-info）→ 写进每台机器一份的档案。
#
# 用法:
#   bash scripts/setup_robot.sh                          # 交互：选机型 → 逐个插设备 → 写档案
#   bash scripts/setup_robot.sh --list                   # 只打印现状（现场设备 + 现有档案），不改动
#   bash scripts/setup_robot.sh --machine pc16 --dry-run # 只打印将写入的补丁，不改动
#   bash scripts/setup_robot.sh --machine pc16 --can     # 写完档案顺带按档案激活 CAN（需要 sudo）
#
# 前置: 无需环境变量——配置 / 日志根 = $MOTRIX_ROBOT_PIPELINE_DIR，未设时回落 <cwd>/motrix-robot-pipeline
# 产物: <根>/config/robot/<machine>.yml（只写「这台机器不同」的键）
#       <根>/config/<机型>.yml（--target config：整份机器配置，即 robot server 直接读的那份）
# 生效: robot_server 启动时读 <根>/config（播种包内示例 + 按 --machine / MOTRIX_ROBOT_PIPELINE_MACHINE / hostname 叠加档案）
# 实现: src/config/setup.py（交互逻辑）+ src/config/probe.py（设备枚举，只读）

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
    cat <<'EOF'
用法: bash scripts/setup_robot.sh [--config <机型>] [--machine <机器名>] [--target profile|config] [--list] [--dry-run] [--can]

  --config <机型>     机型配置名（如 dual_piper；缺省弹出菜单选择）
  --machine <机器名>  档案名；缺省 $MOTRIX_ROBOT_PIPELINE_MACHINE，再缺省 hostname
  --target <位置>     profile（默认）= <根>/config/robot/<machine>.yml（只写差异）；
                      config = <根>/config/<机型>.yml（整份机器配置，即 robot server 直接读的那份）
  --list              只打印现状（现场设备 + 现有档案），不改动
  --dry-run           只打印将写入的补丁，不改动
  --can               写完后顺带执行 sudo scripts/can_muti_activate.sh（按同一份 robot 配置读 can.bindings）
  -h, --help          显示本帮助

现场流程：脚本逐个提示设备（左从臂 / 右从臂 / 主手 / 各相机），**每次只插提示的那一个**，
插好后回车即可——识别靠「插拔差分」，所以不必事先知道序列号或 bus-info。
EOF
}

# ---- 参数：--can 是本脚本独有的开关（其余原样透传给 src/config/setup.py）----
CAN_MODE=false
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --can)
            CAN_MODE=true
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done

# ---- python：能 ``import yaml`` 才用（项目 venv 未 uv sync / 系统 python 无 pyyaml 都不算数）----
PYTHON_CMD=()
for candidate in "$ROOT_DIR/.venv/bin/python" python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c "import yaml" 2>/dev/null; then
        PYTHON_CMD=("$candidate")
        break
    fi
done
if [ ${#PYTHON_CMD[@]} -eq 0 ] && command -v uv >/dev/null && uv run python -c "import yaml" 2>/dev/null; then
    PYTHON_CMD=(uv run python) # 最后手段：按 pyproject 解析依赖（可能触发同步）
fi
if [ ${#PYTHON_CMD[@]} -eq 0 ]; then
    echo "❌ [ERROR]: 找不到可用的 python（需要 pyyaml）：先 uv sync 生成 .venv" >&2
    exit 2
fi

# 根目录（配置 / 日志）：$MOTRIX_ROBOT_PIPELINE_DIR，未设 → <cwd>/motrix-robot-pipeline
echo "==> 根目录：${MOTRIX_ROBOT_PIPELINE_DIR:-$PWD/motrix-robot-pipeline}（config/ + logs/）"

# ---- 解析 --config / --machine / --target（供 --can 复用：与写档同一份目标）----
CONFIG_NAME=""
MACHINE="${MOTRIX_ROBOT_PIPELINE_MACHINE:-$(hostname)}"
TARGET_CONFIG=false
for i in "${!ARGS[@]}"; do
    case "${ARGS[$i]}" in
        --config)
            [ -n "${ARGS[$((i + 1))]:-}" ] && CONFIG_NAME="${ARGS[$((i + 1))]}"
            ;;
        --config=*)
            CONFIG_NAME="${ARGS[$i]#--config=}"
            ;;
        --machine)
            [ -n "${ARGS[$((i + 1))]:-}" ] && MACHINE="${ARGS[$((i + 1))]}"
            ;;
        --machine=*)
            MACHINE="${ARGS[$i]#--machine=}"
            ;;
        --target)
            [ "${ARGS[$((i + 1))]:-}" = "config" ] && TARGET_CONFIG=true
            ;;
        --target=config)
            TARGET_CONFIG=true
            ;;
    esac
done

PYTHONPATH="$ROOT_DIR/src" "${PYTHON_CMD[@]}" -m config.setup "${ARGS[@]}"

if [ "$CAN_MODE" = true ]; then
    if [ "$TARGET_CONFIG" = true ] && [ -z "$CONFIG_NAME" ]; then
        echo "⚠️ --target config 但未给 --config <机型>：CAN 脚本无法定位配置，跳过激活（写完后手动跑：" >&2
        echo "   sudo bash scripts/can_muti_activate.sh --config <机型>）" >&2
        exit 2
    fi
    CAN_ARGS=(--machine "$MACHINE")
    [ -n "$CONFIG_NAME" ] && CAN_ARGS+=(--config "$CONFIG_NAME")
    echo "==> 按 robot 配置激活 CAN（config=${CONFIG_NAME:-（未指定 → 机器档案）} machine=$MACHINE）"
    sudo bash "$ROOT_DIR/scripts/can_muti_activate.sh" "${CAN_ARGS[@]}"
fi
