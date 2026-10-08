#!/bin/bash
# 把机械臂插的 **USB 物理口**绑定到**固定 CAN 名**（left / right / m_left / m_right）：换设备、重启、
# 换顺序都不用改配置。
#
# 绑定表来自 **robot 配置里的 ``can.bindings``**（与 robot server **同一套加载**：读 ``<根>/config/`` 下的
# ``<机型>.yml``（首次访问播种包内示例），再叠加机器档案 ``robot/<machine>.yml``）：
#       can:
#         bindings:
#           "3-2.2:1.0": "left:1000000"   # USB 物理口(bus-info) -> <目标CAN名>:<波特率>
# 生成 / 更新：bash scripts/setup_robot.sh（交互式逐个插设备自动填；--target config 写进机型 yml）
#
# 用法: sudo bash scripts/can_muti_activate.sh [--config <机型|yml>] [--machine <名>] [--list] [--dry-run] [--ignore]
#   缺省：按 $MOTRIX_ROBOT_PIPELINE_CFG（机型名）→ 否则机器档案 <根>/config/robot/${MOTRIX_ROBOT_PIPELINE_MACHINE:-$(hostname)}.yml
#   <根> = $MOTRIX_ROBOT_PIPELINE_DIR，未设时回落 <cwd>/motrix-robot-pipeline

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
declare -A USB_PORTS # 由配置里的 can.bindings 填充（见 load_bindings）
CONFIG=""           # 机型名（如 dual_piper）或 yml 路径（--config）；空 → 由 python 按环境变量 / 机器档案解析
MACHINE=""          # 机器档案名（--machine）；空 → 由 python 按 MOTRIX_ROBOT_PIPELINE_MACHINE / hostname 解析

# ---------------- 参数 ----------------
IGNORE_CHECK=false # --ignore：跳过「CAN 接口数 == 配置条数」的交互确认
LIST_ONLY=false    # --list：只打印 现状 ↔ 配置 对照表
DRY_RUN=false      # --dry-run：只打印将执行的 ip 命令

usage() {
    cat <<'EOF'
用法: sudo bash scripts/can_muti_activate.sh [--config <机型|yml>] [--machine <名>] [--list] [--dry-run] [--ignore]

  --config <机型|yml>  机型名（如 dual_piper → 读 <根>/config/dual_piper.yml + 机器档案叠加，
                       与 robot server 同一份）或 yml 路径；缺省 $MOTRIX_ROBOT_PIPELINE_CFG
  --machine <名>       机器档案名（<根>/config/robot/<名>.yml）；缺省 $MOTRIX_ROBOT_PIPELINE_MACHINE / hostname
  --list      只打印 现状 ↔ 配置 对照表（不改动，无需 root）
  --dry-run   打印将要执行的 ip 命令（不改动，无需 root）
  --ignore    跳过「CAN 接口数 == 配置条数」的交互确认
  -h, --help  显示本帮助

can.bindings（robot 配置里）：**键** = USB 物理口 bus-info（ethtool -i <canX> | grep bus-info），
**值** = <目标接口名>:<波特率>。同一物理口稳定、与设备无关；换 USB 口 / 换 HUB 会变——
重新跑 bash scripts/setup_robot.sh 即可（或看 --list 核对后手改配置）。

注意：绑定过程会 down + 改名接口，执行前请确认机械臂已停止、没有正在跑的 CAN 通信。
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --config)
            if [ -z "${2:-}" ]; then
                echo "❌ [ERROR]: --config 需要一个机型名或 yml 路径" >&2
                exit 2
            fi
            CONFIG="$2"
            shift 2
            ;;
        --machine)
            if [ -z "${2:-}" ]; then
                echo "❌ [ERROR]: --machine 需要一个机器名" >&2
                exit 2
            fi
            MACHINE="$2"
            shift 2
            ;;
        --list)
            LIST_ONLY=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --ignore)
            IGNORE_CHECK=true
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "❌ [ERROR]: 未知参数 '$1'" >&2
            usage >&2
            exit 2
            ;;
    esac
done

# ---------------- 依赖 / 权限 ----------------
die() {
    echo "❌ [ERROR]: $*" >&2
    exit 1
}

command -v ip >/dev/null || die "缺少 ip（iproute2）"
command -v ethtool >/dev/null || die "缺少 ethtool（安装：sudo apt install ethtool）"
if [ "$LIST_ONLY" = false ] && [ "$DRY_RUN" = false ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
    die "改接口名 / 波特率需要 root：请用 sudo 运行（看现状用 --list，预演用 --dry-run）"
fi

# ---------------- 工具函数 ----------------
FAILED_CMDS=0 # 失败的 ip / ethtool 命令数（收尾据此返回非零）

run() {
    # 执行一条改动命令：dry-run 只打印；失败则记录（不中断，收尾统一报告）
    if [ "$DRY_RUN" = true ]; then
        echo "      [dry-run] $*"
        return 0
    fi
    if "$@"; then
        return 0
    fi
    echo "      ❌ [ERROR]: 执行失败 → $*"
    FAILED_CMDS=$((FAILED_CMDS + 1))
    return 1
}

bus_info_of() { ethtool -i "$1" 2>/dev/null | awk -F': ' '/^bus-info/{print $2}'; }
bitrate_of() { ip -details link show "$1" 2>/dev/null | sed -n 's/.*bitrate \([0-9]\+\).*/\1/p' | head -1; }
is_up() { ip -br link show "$1" 2>/dev/null | grep -qw UP && echo yes || echo no; }

# ---------------- 机器档案 → 绑定表 ----------------
# 选 python：能 ``import yaml`` 才用（项目 venv 未 uv sync / 系统 python 无 pyyaml 都不算数）
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

resolve_profile_args() {
    # 拼 python 参数：--dump-bindings [<机型|yml>] [--machine <名>]（不传值 → 由 python 解析机器档案）
    DUMP_ARGS=(--dump-bindings)
    [ -n "$CONFIG" ] && DUMP_ARGS+=("$CONFIG")
    [ -n "$MACHINE" ] && DUMP_ARGS+=(--machine "$MACHINE")
}

load_bindings() {
    # robot 配置的 can.bindings → USB_PORTS（每行 "<bus-info>\t<目标名>:<波特率>"）
    local dump
    [ ${#PYTHON_CMD[@]} -gt 0 ] || die "找不到可用的 python（需要 pyyaml）：先 uv sync"
    resolve_profile_args
    if ! dump="$(PYTHONPATH="$ROOT_DIR/src" "${PYTHON_CMD[@]}" -m config.setup "${DUMP_ARGS[@]}")"; then
        die "读 robot 配置失败（见上面报错）：config=${CONFIG:-（按 MOTRIX_ROBOT_PIPELINE_CFG / 机器档案）} machine=${MACHINE:-（MOTRIX_ROBOT_PIPELINE_MACHINE / hostname）}"
    fi
    while IFS=$'\t' read -r port value; do
        [ -n "$port" ] && USB_PORTS["$port"]="$value"
    done <<<"$dump"
    [ "${#USB_PORTS[@]}" -gt 0 ] || die "配置里的 can.bindings 为空（用 bash scripts/setup_robot.sh 生成）"
    echo "[INFO]: 绑定表来自 robot 配置（${CONFIG:+config=$CONFIG }machine=${MACHINE:-MOTRIX_ROBOT_PIPELINE_MACHINE / hostname}，${#USB_PORTS[@]} 条）"
}

load_bindings

# ---------------- 配置校验（值格式 + 目标名唯一）----------------
declare -A PORT_TARGET PORT_BITRATE
declare -A NAME_OWNER # 目标名 → 端口（重复检测；必须 declare -A，写成 NAME_OWNER=() 会变成索引数组）
for port in "${!USB_PORTS[@]}"; do
    value="${USB_PORTS[$port]}"
    if [[ ! "$value" =~ ^([^:]+):([0-9]+)$ ]]; then
        die "can.bindings[\"$port\"]=\"$value\" 格式不正确，应为 <目标名>:<波特率>（如 left:1000000）"
    fi
    name="${BASH_REMATCH[1]}"
    br="${BASH_REMATCH[2]}"
    if [ -n "${NAME_OWNER[$name]:-}" ]; then
        die "重复的目标 CAN 名 '$name'（出现在端口 $port 与 ${NAME_OWNER[$name]}）—— 请先改名"
    fi
    NAME_OWNER["$name"]="$port"
    PORT_TARGET["$port"]="$name"
    PORT_BITRATE["$port"]="$br"
done
PREDEFINED_COUNT=${#USB_PORTS[@]}
[ "$PREDEFINED_COUNT" -gt 0 ] || die "绑定表为空：先用 bash scripts/setup_robot.sh 生成机器档案的 can.bindings"

# ---------------- 系统现状采集 ----------------
mapfile -t SYS_IFACES < <(ip -br link show type can 2>/dev/null | awk 'NF {print $1}')
CURRENT_CAN_COUNT=${#SYS_IFACES[@]}

# ---------------- --list：只打印 现状 ↔ 配置 对照表 ----------------
if [ "$LIST_ONLY" = true ]; then
    echo "🔧 配置（USB 物理口 → 目标 CAN 名，共 $PREDEFINED_COUNT 条）:"
    printf '  %-16s → %-10s %s\n' "PORT(bus-info)" "TARGET" "BITRATE"
    for port in "${!USB_PORTS[@]}"; do
        printf '  %-16s → %-10s %s\n' "$port" "${PORT_TARGET[$port]}" "${PORT_BITRATE[$port]}"
    done

    echo
    echo "🔍 系统当前 CAN 接口（$CURRENT_CAN_COUNT 个）:"
    if [ "$CURRENT_CAN_COUNT" -eq 0 ]; then
        echo "  （未检测到：确认 USB-CAN 已插好、驱动已加载 → sudo modprobe gs_usb）"
    else
        printf '  %-10s %-16s %-10s %-6s %s\n' "IFACE" "PORT(bus-info)" "BITRATE" "LINK" "→ TARGET"
        for iface in "${SYS_IFACES[@]}"; do
            bi="$(bus_info_of "$iface")"
            br="$(bitrate_of "$iface")"
            printf '  %-10s %-16s %-10s %-6s %s\n' "$iface" "${bi:--}" "${br:--}" "$(is_up "$iface")" "${PORT_TARGET[$bi]:-（未配置）}"
        done
    fi

    echo
    echo "⚠️  配置里声明、但系统未出现的端口:"
    missing=0
    for port in "${!USB_PORTS[@]}"; do
        found=false
        for iface in "${SYS_IFACES[@]}"; do
            if [ "$(bus_info_of "$iface")" = "$port" ]; then
                found=true
                break
            fi
        done
        if [ "$found" = false ]; then
            echo "  - $port（${PORT_TARGET[$port]}）"
            missing=$((missing + 1))
        fi
    done
    [ "$missing" -eq 0 ] && echo "  （无）"
    exit 0
fi

# ---------------- 数量校验（系统接口数 vs 配置条数）----------------
if [ "$CURRENT_CAN_COUNT" -eq 0 ]; then
    die "未检测到任何 CAN 接口：确认 USB-CAN 已插好、驱动已加载（sudo modprobe gs_usb）"
fi

if [ "$IGNORE_CHECK" = false ] && [ "$CURRENT_CAN_COUNT" -ne "$PREDEFINED_COUNT" ]; then
    echo "[WARN]: 检测到 $CURRENT_CAN_COUNT 个 CAN 接口 != 配置 $PREDEFINED_COUNT 条"
    if [ ! -t 0 ]; then
        die "stdin 不是终端（非交互）不做确认：修好接线 / 配置后重跑，或加 --ignore 强制继续"
    fi
    read -r -p "是否继续？(y/N): " user_input
    case "$user_input" in
        [yY] | [yY][eE][sS]) echo "继续执行..." ;;
        *)
            echo "已取消。"
            exit 1
            ;;
    esac
else
    echo "CAN 数量校验已跳过或匹配（$CURRENT_CAN_COUNT / $PREDEFINED_COUNT），继续..."
fi

# ---------------- 绑定：逐接口对齐 USB 口 → 目标名 ----------------
for iface in "${SYS_IFACES[@]}"; do
    bi="$(bus_info_of "$iface")"
    echo "--------------------------- $iface（端口 ${bi:--}）"
    if [ -z "$bi" ]; then
        echo "  [WARN]: 取不到 bus-info（ethtool -i $iface 失败），跳过"
        echo "-----------------------------------------------------------------"
        continue
    fi
    target="${PORT_TARGET[$bi]:-}"
    if [ -z "$target" ]; then
        echo "  [WARN]: 该 USB 口不在配置里，未处理（配置里的口：${!USB_PORTS[*]}）"
        echo "-----------------------------------------------------------------"
        continue
    fi
    want="${PORT_BITRATE[$bi]}"
    cur_br="$(bitrate_of "$iface")"
    cur_up="$(is_up "$iface")"
    echo "  [INFO]: 目标 $target @ $want（当前 link=$cur_up bitrate=${cur_br:-未设置}）"

    if [ "$cur_up" = yes ] && [ "${cur_br:-}" = "$want" ]; then
        if [ "$iface" = "$target" ]; then
            echo "  [INFO]: 已就位：$target（$want / up）"
        else
            echo "  [INFO]: 波特率已正确，重命名 $iface → $target"
            run ip link set "$iface" down \
                && run ip link set "$iface" name "$target" \
                && run ip link set "$target" up
        fi
    elif ip link show "$target" &>/dev/null; then
        echo "  ❌ [WARN]: 目标名 $target 已被别的接口占用，跳过 $iface"
        echo "  [HINT]: 先处理占用该名字的接口 / 断开对应设备，再重跑"
        echo "-----------------------------------------------------------------"
        continue
    else
        echo "  [INFO]: 设置波特率 $want 并重命名 $iface → $target"
        run ip link set "$iface" down \
            && run ip link set "$iface" type can bitrate "$want" \
            && { [ "$iface" = "$target" ] || run ip link set "$iface" name "$target"; } \
            && run ip link set "$target" up
    fi
    echo "-----------------------------------------------------------------"
done

# ---------------- 收尾核对：目标态 vs 实际态（成功只认核对通过）----------------
echo
echo "📋 目标态 vs 实际态:"
printf '  %-10s %-16s %-10s %-10s %s\n' "TARGET" "PORT(bus-info)" "WANT" "GOT" "STATE"

if [ "$DRY_RUN" = true ]; then
    for port in "${!USB_PORTS[@]}"; do
        printf '  %-10s %-16s %-10s %-10s %s\n' \
            "${PORT_TARGET[$port]}" "$port" "${PORT_BITRATE[$port]}" "-" "dry-run（未改动）"
    done
    echo
    echo "[RESULT]: 🧪 dry-run 结束：以上命令均未真正执行（去掉 --dry-run 生效）"
    exit 0
fi

OK_COUNT=0
BAD_COUNT=0
for port in "${!USB_PORTS[@]}"; do
    target="${PORT_TARGET[$port]}"
    want="${PORT_BITRATE[$port]}"
    got="$(bitrate_of "$target")"
    up="$(is_up "$target")"
    if [ "$up" = yes ] && [ "${got:-}" = "$want" ]; then
        printf '  %-10s %-16s %-10s %-10s %s\n' "$target" "$port" "$want" "$got" "✅ ok"
        OK_COUNT=$((OK_COUNT + 1))
    else
        printf '  %-10s %-16s %-10s %-10s %s\n' "$target" "$port" "$want" "${got:--}" "❌ 未达成（link=$up）"
        BAD_COUNT=$((BAD_COUNT + 1))
    fi
done

echo
if [ "$BAD_COUNT" -eq 0 ] && [ "$FAILED_CMDS" -eq 0 ]; then
    echo "[RESULT]: ✅ $OK_COUNT/$PREDEFINED_COUNT 个目标 CAN 名就位（名字 + up + 波特率全部核对通过）"
    exit 0
fi
echo "[RESULT]: ❌ 未达成 $BAD_COUNT/$PREDEFINED_COUNT 个目标（ip 命令失败 $FAILED_CMDS 次）"
exit 1
