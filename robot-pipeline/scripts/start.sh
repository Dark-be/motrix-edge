#!/bin/bash
# 启动机器人进程服务器（通用入口，按 config 自动匹配机器人）。
#
# 用法:
#   bash scripts/start.sh                                  # 交互菜单选择 src/config/*.yml（非交互回退默认）
#   bash scripts/start.sh test_robot_server                # 直接指定配置名（.yml 后缀可省略，跳过菜单）
#   bash scripts/start.sh dual_piper_server.yml --host 0.0.0.0 --port 8090
#   bash scripts/start.sh --help                           # 查看 server 参数说明（用默认配置）
#
# 说明:
#   - 配置放 src/config/（包内默认）；首个非“-”开头的参数视为配置名，其余参数透传给 server
#   - 未指定配置名且 stdin 是交互终端时，弹出数字菜单供选择；否则回退默认 test_robot_server.yml
#   - 等价命令: uv run python src/server/robot_server.py --config <name> [args...]

set -euo pipefail

# 定位项目根（本脚本位于 <root>/scripts/）
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# 特殊：--help / -h 直接透传（用默认配置让 server 打印参数说明，不弹选择菜单）
if [ $# -gt 0 ] && { [ "$1" = "--help" ] || [ "$1" = "-h" ]; }; then
    echo "==> 查看 server 参数说明（默认配置 test_robot_server.yml）"
    exec uv run python src/server/robot_server.py --config "test_robot_server.yml" "$@"
fi

# 首个非选项参数为配置名（若显式指定则跳过菜单）
CONFIG_NAME=""
if [ $# -gt 0 ] && [[ "$1" != -* ]]; then
    CONFIG_NAME="$1"
    shift
fi

# 未指定配置名：交互菜单选择 src/config/*.yml
if [ -z "$CONFIG_NAME" ]; then
    mapfile -t AVAILABLE_CFGS < <(ls "$ROOT_DIR"/src/config/*.yml 2>/dev/null | xargs -n1 basename | sort)
    if [ "${#AVAILABLE_CFGS[@]}" -eq 0 ]; then
        echo "错误：src/config/ 下没有可用的 *.yml 配置" >&2
        exit 1
    fi

    if [ -t 0 ]; then
        echo "请选择要启动的机器人配置："
        select CONFIG_NAME in "${AVAILABLE_CFGS[@]}"; do
            if [ -n "$CONFIG_NAME" ]; then
                break
            fi
            echo "无效选择，请输入列表中的编号（1-${#AVAILABLE_CFGS[@]}）。" >&2
        done
    else
        # 非交互环境（管道 / CI / 脚本内调用）无菜单可用，回退默认配置
        CONFIG_NAME="test_robot_server.yml"
        echo "==> 非交互环境：使用默认配置 $CONFIG_NAME（可用：${AVAILABLE_CFGS[*]}）" >&2
    fi
fi

# 自动补全 .yml 后缀
case "$CONFIG_NAME" in
    *.yml) ;;
    *) CONFIG_NAME="$CONFIG_NAME.yml" ;;
esac

# 校验配置文件存在
if [ ! -f "$ROOT_DIR/src/config/$CONFIG_NAME" ]; then
    echo "错误：找不到配置文件 src/config/$CONFIG_NAME" >&2
    echo "可用配置：" >&2
    ls "$ROOT_DIR"/src/config/*.yml 2>/dev/null | xargs -n1 basename >&2
    exit 1
fi

echo "==> 启动机器人服务器：config=$CONFIG_NAME"
echo "==> 命令: uv run python src/server/robot_server.py --config $CONFIG_NAME $*"
exec uv run python src/server/robot_server.py --config "$CONFIG_NAME" "$@"
