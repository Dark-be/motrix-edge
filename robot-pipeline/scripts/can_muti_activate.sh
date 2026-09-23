#!/bin/bash
declare -A USB_PORTS

# 键 = USB 物理口 bus-info（ethtool -i <canX> | grep bus-info）；值 = <目标名>:<波特率>
# 同一物理口稳定、与设备无关；换 USB 口 / 换 HUB 会变（用 --list 核对后再填）
USB_PORTS["3-2.2:1.0"]="left:1000000"
USB_PORTS["3-2.1:1.0"]="right:1000000"
USB_PORTS["3-1.3:1.0"]="m_left:1000000"
USB_PORTS["3-1.5:1.0"]="m_right:1000000"

# ---------------- 参数 ----------------
IGNORE_CHECK=false # --ignore：跳过「CAN 接口数 == 配置条数」的交互确认
LIST_ONLY=false    # --list：只打印 现状 ↔ 配置 对照表
DRY_RUN=false      # --dry-run：只打印将执行的 ip 命令

usage() {
    cat <<'EOF'
用法: sudo bash scripts/can_muti_activate.sh [--list] [--dry-run] [--ignore]

  --list      只打印 现状 ↔ 配置 对照表（不改动，无需 root）
  --dry-run   打印将要执行的 ip 命令（不改动，无需 root）
  --ignore    跳过「CAN 接口数 == 配置条数」的交互确认
  -h, --help  显示本帮助

USB_PORTS（本脚本顶部）：键 = USB 物理口 bus-info（ethtool -i <canX> | grep bus-info），
值 = <目标接口名>:<波特率>。

注意：绑定过程会 down + 改名接口，执行前请确认机械臂已停止、没有正在跑的 CAN 通信。
EOF
}

for arg in "$@"; do
    case "$arg" in
        --list) LIST_ONLY=true ;;
        --dry-run) DRY_RUN=true ;;
        --ignore) IGNORE_CHECK=true ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "❌ [ERROR]: 未知参数 '$arg'" >&2
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

# ---------------- 配置校验（值格式 + 目标名唯一）----------------
declare -A PORT_TARGET PORT_BITRATE
declare -A NAME_OWNER # 目标名 → 端口（重复检测；必须 declare -A，写成 NAME_OWNER=() 会变成索引数组）
for port in "${!USB_PORTS[@]}"; do
    value="${USB_PORTS[$port]}"
    if [[ ! "$value" =~ ^([^:]+):([0-9]+)$ ]]; then
        die "USB_PORTS[\"$port\"]=\"$value\" 格式不正确，应为 <目标名>:<波特率>（如 left:1000000）"
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
[ "$PREDEFINED_COUNT" -gt 0 ] || die "USB_PORTS 为空：先在脚本顶部配置 <USB 口>=<目标名>:<波特率>"

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
