#!/usr/bin/env bash
# 在干净的 Alibaba Cloud Linux 3（2C2G）上把 genai-mindmap 跑起来。
#
#   sudo bash deploy/install.sh --domain mindmap.example.com
#   sudo bash deploy/install.sh --domain mindmap.example.com --with-office
#
# 做四件事：系统依赖 → 服务账号与目录 → Python 3.12（uv 装的独立解释器）与依赖
# → systemd + nginx 配置。前端 dist 不在这里构建（2C2G 上装 node 再打包太慢），
# 由本机 `npm run build` 后 scp 上来，脚本最后会打印目标路径。
#
# 脚本可重复执行：目录存在就跳过、配置覆盖、密码不重设。
set -euo pipefail

DOMAIN=""
WITH_OFFICE=0
WITH_CERTBOT=0
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --with-office) WITH_OFFICE=1; shift ;;
    --with-certbot) WITH_CERTBOT=1; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [[ -z "$DOMAIN" ]]; then
  echo "必须给 --domain，例如：--domain mindmap.example.com" >&2
  exit 2
fi
if [[ $EUID -ne 0 ]]; then
  echo "请用 sudo 运行" >&2
  exit 2
fi

APP_USER=genai
OPT_DIR=/opt/genai-mindmap
DATA_DIR=/var/lib/genai-mindmap
ENV_DIR=/etc/genai-mindmap
BACKUP_DIR=/var/backups/genai-mindmap
WEB_ROOT="$OPT_DIR/frontend/dist"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "1/6 系统依赖"
# nginx：静态文件 + 反代；httpd-tools 提供 htpasswd；sqlite 用于在线备份；
# git/tar 用于拉代码；policycoreutils-python-utils 提供 semanage（SELinux 放行端口）。
dnf install -y nginx git tar httpd-tools sqlite policycoreutils-python-utils \
  gcc gcc-c++ make openssl-devel libffi-devel

if [[ $WITH_OFFICE -eq 1 ]]; then
  # .doc / .ppt 需要 LibreOffice，约 1.5GB；不带这个参数时不装，
  # 这两个格式上传会返回 422 并提示安装命令。
  dnf install -y libreoffice-writer libreoffice-impress
fi

say "2/6 交换分区（2G 内存上装依赖与解析 PDF 都需要余量）"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "已创建 2G 交换分区"
else
  echo "已有交换分区，跳过"
fi

say "3/6 服务账号与目录"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /sbin/nologin "$APP_USER"
mkdir -p "$OPT_DIR" "$DATA_DIR" "$ENV_DIR" "$BACKUP_DIR" "$WEB_ROOT"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR" "$BACKUP_DIR" "$WEB_ROOT"
chmod 750 "$DATA_DIR" "$BACKUP_DIR"

# 代码：脚本从仓库根运行，直接把工作目录内容同步过去（排除本地数据与前端依赖）
if [[ "$REPO_DIR" != "$OPT_DIR" ]]; then
  tar -C "$REPO_DIR" \
    --exclude='.git' --exclude='frontend/node_modules' --exclude='frontend/dist' \
    --exclude='backend/.venv' --exclude='backend/data' --exclude='backend/__pycache__' \
    -cf - . | tar -C "$OPT_DIR" -xf -
  chown -R "$APP_USER:$APP_USER" "$OPT_DIR"
fi

# 环境变量文件：密钥运行时注入，仓库里不存
if [[ ! -f "$ENV_DIR/env" ]]; then
  cat > "$ENV_DIR/env" <<'ENVFILE'
# 运行时配置，改完 systemctl restart genai-mindmap 生效。权限 0640 root:genai。
MINDMAP_LLM_MODE=model
MINDMAP_LLM_BASE_URL=
MINDMAP_LLM_API_KEY=
MINDMAP_LLM_MODEL=
MINDMAP_DB_PATH=/var/lib/genai-mindmap/mindmaps.db
DASHSCOPE_API_KEY=
MINDMAP_AGENT_ENABLED=1
MINDMAP_NLI_ENABLED=0
ENVFILE
  echo "已生成 $ENV_DIR/env —— 记得填 MINDMAP_LLM_* 四项"
else
  echo "$ENV_DIR/env 已存在，保留不动"
fi
chown root:"$APP_USER" "$ENV_DIR/env"
chmod 640 "$ENV_DIR/env"

say "4/6 Python 3.12 与依赖（uv 管理，避开系统 python3.6/3.8）"
if ! command -v uv >/dev/null 2>&1; then
  # uv 自带下载器，比 pip 快很多；装在 /usr/local/bin 供 genai 用户使用
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi
sudo -u "$APP_USER" env HOME="$OPT_DIR" /usr/local/bin/uv python install 3.12
if [[ ! -x "$OPT_DIR/.venv/bin/python" ]]; then
  sudo -u "$APP_USER" env HOME="$OPT_DIR" /usr/local/bin/uv venv --python 3.12 "$OPT_DIR/.venv"
fi
# docling 会带进 torch（CPU 版），首次安装 5–10 分钟、占 2GB 左右磁盘
sudo -u "$APP_USER" env HOME="$OPT_DIR" /usr/local/bin/uv pip install \
  --python "$OPT_DIR/.venv/bin/python" -r "$OPT_DIR/backend/requirements.txt"

say "5/6 systemd"
install -m 644 "$REPO_DIR/deploy/genai-mindmap.service" /etc/systemd/system/genai-mindmap.service
systemctl daemon-reload
systemctl enable --now genai-mindmap

say "6/6 nginx"
mkdir -p /etc/nginx/conf.d /var/www/html
sed -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__WEB_ROOT__|$WEB_ROOT|g" \
  "$REPO_DIR/deploy/nginx.conf" > /etc/nginx/conf.d/genai-mindmap.conf

# 默认账号 demo，密码随机打印一次；重复执行不会改已有密码
if [[ ! -f /etc/nginx/.htpasswd-mindmap ]]; then
  PASS="$(openssl rand -base64 12)"
  htpasswd -cb /etc/nginx/.htpasswd-mindmap demo "$PASS"
  chmod 640 /etc/nginx/.htpasswd-mindmap
  chown root:nginx /etc/nginx/.htpasswd-mindmap 2>/dev/null || true
  echo "已创建 Basic Auth 账号：demo / $PASS   （只显示这一次）"
else
  echo "已有 /etc/nginx/.htpasswd-mindmap，保留不动；要加账号用 htpasswd -b <文件> <用户名> <密码>"
fi

# SELinux 默认不允许 nginx 反代到本地 socket/端口；AL3 默认 enforcing
setsebool -P httpd_can_network_connect 1 2>/dev/null || true

nginx -t
systemctl enable --now nginx
systemctl reload nginx

# 前端 dist 是 scp 上来的，默认权限可能是 600，nginx（用户 nginx）读不到就是 403
chmod -R a+rX "$WEB_ROOT" || true

# 备份：每天 03:20 用 sqlite 的在线备份接口导一份，保留 14 天
install -m 755 "$REPO_DIR/deploy/backup.sh" /usr/local/bin/genai-mindmap-backup
echo '20 3 * * * root /usr/local/bin/genai-mindmap-backup >/var/log/genai-mindmap-backup.log 2>&1' \
  > /etc/cron.d/genai-mindmap-backup
chmod 644 /etc/cron.d/genai-mindmap-backup

if [[ $WITH_CERTBOT -eq 1 ]]; then
  say "附带：certbot 签 HTTPS 证书"
  # AL3 的官方源里没有 certbot，走 EPEL
  dnf install -y epel-release || true
  dnf install -y certbot python3-certbot-nginx
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
    --register-unsafely-without-email --redirect || {
      echo "证书签发失败：多半是域名还没解析到本机，或 80 端口没放行。修好后重跑：" >&2
      echo "  certbot --nginx -d $DOMAIN" >&2
    }
  systemctl enable --now certbot-renew.timer 2>/dev/null || true
fi

printf '\n完成。还差三件事：\n'
printf '  1. 填 %s（MINDMAP_LLM_BASE_URL / API_KEY / MODEL），然后 systemctl restart genai-mindmap\n' "$ENV_DIR/env"
printf '  2. 本机 npm run build 后把 frontend/dist 传到 %s\n' "$WEB_ROOT"
printf '  3. 域名解析到本机后签证书：certbot --nginx -d %s\n' "$DOMAIN"
printf '\n自检：curl -u demo:**** http://127.0.0.1/api/health\n'
