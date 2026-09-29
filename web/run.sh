#!/bin/bash
# 在容器里跑前端命令（本机 WSL 里没有 Node）。
#
#   ./run.sh check    类型检查（vue-tsc --noEmit）
#   ./run.sh build    类型检查 + 打包（产物在 dist/）
#   ./run.sh dev      启动 Vite 开发服务器（热重载，端口 5173）
#   ./run.sh install  只装依赖
#
# ── 为什么需要这个脚本，而不是直接 npm run xxx ──────────────────────
#
#   本机 WSL 里**没有原生 Node**（只有 Windows 侧一个 managed node，
#   它的 npm 在 WSL 里跑不起来）。所以前端命令必须跑在容器里。
#
# ── 为什么 node_modules 放在具名卷上，不放在项目目录 ────────────────
#
#   它有三万多个小文件、约 200MB。项目目录在 /mnt/c（DrvFs，9p 文件系统），
#   在那上面读写几万个小文件会慢到无法接受——我们已经实测过 venv 在
#   /mnt/c 上要 72 秒、在 ext4 上只要 1.3 秒，差 55 倍。
#
#   具名卷（iot-web-modules）在 Docker 的 ext4 存储里，天然避开这个问题，
#   而且能跨多次运行缓存——第二次之后 npm ci 只要几秒。
#
# ── 为什么 dev 模式要 --network host ────────────────────────────────
#
#   vite.config.ts 里的 proxy 目标写的是 http://127.0.0.1:8005。
#   不加 --network host 的话，容器里的 127.0.0.1 是**容器自己**，
#   proxy 会连不上后端——症状是页面能打开但数据全是空的。

set -euo pipefail

WEB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_IMAGE=node:22-alpine
MODULES_VOLUME=iot-web-modules

ACTION="${1:-check}"

case "$ACTION" in
  install) CMD="npm ci" ;;
  check)   CMD="npm ci --no-audit --no-fund && npm run typecheck" ;;
  build)   CMD="npm ci --no-audit --no-fund && npm run build" ;;
  dev)     CMD="npm ci --no-audit --no-fund && npm run dev" ;;
  *)
    echo "用法: $0 [install|check|build|dev]" >&2
    exit 2
    ;;
esac

echo "web 目录        : $WEB_DIR"
echo "node_modules 卷 : $MODULES_VOLUME"
echo "动作            : $ACTION"
echo

# dev 需要交互式输出，所以带 -it；其余动作在非交互环境里跑，
# 带 -t 会报 "the input device is not a TTY"。
TTY_FLAG=""
if [ "$ACTION" = "dev" ] && [ -t 0 ]; then
  TTY_FLAG="-it"
fi

# shellcheck disable=SC2086
exec docker run --rm $TTY_FLAG \
  --network host \
  -v "$WEB_DIR":/w \
  -v "$MODULES_VOLUME":/w/node_modules \
  -w /w \
  "$NODE_IMAGE" \
  sh -c "$CMD"
