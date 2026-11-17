#!/usr/bin/env bash
# 部署的唯一特权入口：由 ECS 上的 self-hosted runner（用户 ghrunner）通过 sudo 调用。
#
#   sudo /usr/local/sbin/genai-cd-apply --domain <域名> --version <tag> \
#        --source /tmp/.../source.tar.gz --dist /tmp/.../frontend-dist.tar.gz
#
# 为什么单独做一个脚本：runner 的 sudo 权限只开放给这一个文件，它是 root 拥有、
# 不可被 runner 用户改写的。这样即使 runner 侧被攻破，能做的也只有"装一个包"，
# 而不是任意 root 命令。
set -euo pipefail

REPO_DIR="/home/ghrunner/genai-mindmap"
OPT_DIR="/opt/genai-mindmap"
APP_USER="genai"
DOMAIN=""
VERSION=""
SOURCE=""
DIST=""

die() { printf '出错：%s\n' "$1" >&2; exit 1; }
say() { printf '\n== %s\n' "$1"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)  DOMAIN="${2:-}"; shift 2 ;;
    --version) VERSION="${2:-}"; shift 2 ;;
    --source)  SOURCE="${2:-}"; shift 2 ;;
    --dist)    DIST="${2:-}"; shift 2 ;;
    *) die "未知参数 $1" ;;
  esac
done

[[ "$(id -u)" == "0" ]] || die "必须以 root 运行（sudo）"
[[ -n "$DOMAIN" ]] || die "缺少 --domain"
[[ -n "$VERSION" ]] || die "缺少 --version"
[[ -f "$SOURCE" ]] || die "源码包不存在：$SOURCE"

# 版本号会写进 systemd 环境变量、再经 /api/health 回读，所以只接受 tag 形状的字符串。
[[ "$VERSION" =~ ^[A-Za-z0-9._-]+$ ]] || die "版本号只能包含字母、数字、点、下划线、连字符"

say "解包源码到 $REPO_DIR"
rm -rf "$REPO_DIR"
mkdir -p "$REPO_DIR"
tar -xzf "$SOURCE" -C "$REPO_DIR"
[[ -f "$REPO_DIR/deploy/install.sh" ]] || die "包里没有 deploy/install.sh，源码包不对"

say "安装后端（install.sh 会重启服务并自检版本）"
bash "$REPO_DIR/deploy/install.sh" --domain "$DOMAIN" --version "$VERSION"

if [[ -f "$DIST" ]]; then
  say "发布前端"
  install -d -o "$APP_USER" -g "$APP_USER" "$OPT_DIR/frontend"
  rm -rf "$OPT_DIR/frontend/dist.prev"
  if [[ -d "$OPT_DIR/frontend/dist" ]]; then
    mv "$OPT_DIR/frontend/dist" "$OPT_DIR/frontend/dist.prev"
  fi
  mkdir -p "$OPT_DIR/frontend/dist"
  tar -xzf "$DIST" -C "$OPT_DIR/frontend/dist"
  chown -R "$APP_USER:$APP_USER" "$OPT_DIR/frontend"
  # nginx 以 nginx 用户读静态文件：目录要可进入、文件要可读
  chmod -R a+rX "$OPT_DIR/frontend/dist"
  # /opt 下默认上下文是 usr_t，nginx 读不了；SELinux 关着时 restorecon 会直接报错，所以 || true
  restorecon -R "$OPT_DIR/frontend/dist" 2>/dev/null || true
  nginx -t
  systemctl reload nginx
else
  say "没有前端包，跳过 dist 替换（只更新后端）"
fi

say "自检"
health="$(curl -fsS --max-time 5 http://127.0.0.1:8000/api/health)" || die "本机 /api/health 无响应"
printf '本机：%s\n' "$health"
case "$health" in
  *"$VERSION"*) ;;
  *) die "版本号对不上：期望 $VERSION，实际 $health" ;;
esac
printf '完成：%s\n' "$VERSION"
