#!/bin/bash
# 在 Edge 本机人工检查并安全快进 motrix-edge 仓库；不覆盖本地修改、不重启服务。
#
# 用法：
#   bash scripts/update.sh          # 检查后快进 origin/dev
#   bash scripts/update.sh --check  # 只检查，不更新工作树
#
# 可选环境变量：
#   MOTRIX_EDGE_UPDATE_REMOTE  Git remote，缺省 origin
#   MOTRIX_EDGE_UPDATE_BRANCH  目标分支，缺省 dev

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${MOTRIX_EDGE_UPDATE_REMOTE:-origin}"
BRANCH="${MOTRIX_EDGE_UPDATE_BRANCH:-dev}"
CHECK_ONLY=false

case "${1:-}" in
    "") ;;
    --check) CHECK_ONLY=true ;;
    -h | --help)
        sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "错误：未知参数 $1（仅支持 --check / --help）" >&2
        exit 2
        ;;
esac

if ! [[ "$REMOTE" =~ ^[A-Za-z0-9._-]+$ ]] || [[ "$REMOTE" == -* ]]; then
    echo "错误：非法 remote：$REMOTE" >&2
    exit 2
fi
if ! [[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "$BRANCH" == -* ]] || [[ "$BRANCH" == *..* ]]; then
    echo "错误：非法分支：$BRANCH" >&2
    exit 2
fi

cd "$ROOT_DIR"
if [ ! -e .git ]; then
    echo "错误：$ROOT_DIR 不是 Git 工作树" >&2
    exit 2
fi

CURRENT_BRANCH="$(git branch --show-current)"
DIRTY="$(git status --porcelain)"
REMOTE_REF="$REMOTE/$BRANCH"
FETCH_REF="+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"

echo "==> 获取 $REMOTE_REF"
git fetch --prune "$REMOTE" "$FETCH_REF"

read -r AHEAD BEHIND < <(git rev-list --left-right --count "HEAD...$REMOTE_REF")
echo "==> 当前分支：${CURRENT_BRANCH:-（detached HEAD）}"
echo "==> 目标分支：$BRANCH"
echo "==> 提交差异：领先 $AHEAD · 落后 $BEHIND"
if [ -n "$DIRTY" ]; then
    echo "==> 工作树：有本地修改"
else
    echo "==> 工作树：干净"
fi

if [ "$CHECK_ONLY" = true ]; then
    exit 0
fi
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    echo "拒绝更新：当前不在 $BRANCH 分支，请人工确认。" >&2
    exit 3
fi
if [ -n "$DIRTY" ]; then
    echo "拒绝更新：工作树有本地修改，请人工处理。" >&2
    exit 3
fi
if [ "$AHEAD" -ne 0 ]; then
    echo "拒绝更新：本地领先或与远端分叉，请人工处理。" >&2
    exit 3
fi
if [ "$BEHIND" -eq 0 ]; then
    echo "==> 已是最新，无需更新"
    exit 0
fi

BEFORE="$(git rev-parse HEAD)"
git merge --ff-only "$REMOTE_REF"
AFTER="$(git rev-parse HEAD)"
echo "==> 更新完成：${BEFORE:0:8} → ${AFTER:0:8}"
echo "==> 本脚本不更新依赖、不重启 Edge；请按部署流程人工执行后续操作。"
