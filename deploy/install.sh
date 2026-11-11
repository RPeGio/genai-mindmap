#!/usr/bin/env bash
# 在干净的 Alibaba Cloud Linux 3（2C2G）上把 genai-mindmap 跑起来。
#
#   sudo bash deploy/install.sh --domain mindmap.example.com
#   sudo bash deploy/install.sh --domain mindmap.example.com --with-office
#   sudo bash deploy/install.sh --domain mindmap.example.com --with-certbot
#
# 做四件事：系统依赖 → 服务账号与目录 → Python 3.12（uv 装的独立解释器）与依赖
# → systemd + nginx 配置。前端 dist 不在这里构建（2C2G 上装 node 再打包太慢），
# 由本机 `npm run build` 后 scp 上来，脚本最后会打印目标路径。
#
# 脚本可重复执行：目录存在就跳过、配置覆盖、密码不重设。覆盖配置之前会把当前
# 代码快照到 /var/lib/genai-mindmap/releases/，回滚时直接从那里解包（见 DEPLOY 第 8 节）。
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
RELEASE_DIR="$DATA_DIR/releases"
LOG_DIR=/var/log/genai-mindmap
WEB_ROOT="$OPT_DIR/frontend/dist"
CERT_DIR="/etc/letsencrypt/live/$DOMAIN"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "1/6 系统依赖"
# nginx：静态文件 + 反代；httpd-tools 提供 htpasswd；sqlite 用于在线备份；
# git/tar 用于拉代码；policycoreutils-python-utils 提供 semanage（SELinux 上下文）。
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
# 日志目录：systemd 单元的 LogsDirectory 也会建它，但 cron 的备份日志直接写这里，
# 而且首次启动前目录不存在会让单元以 226/NAMESPACE 失败，所以先建。
mkdir -p "$OPT_DIR" "$DATA_DIR" "$ENV_DIR" "$BACKUP_DIR" "$RELEASE_DIR" "$LOG_DIR" "$WEB_ROOT"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR" "$BACKUP_DIR" "$WEB_ROOT"
chown "$APP_USER:$APP_USER" "$LOG_DIR"
chmod 750 "$DATA_DIR" "$BACKUP_DIR" "$LOG_DIR"

# 代码：脚本从仓库根运行，直接把工作目录内容同步过去（排除本地数据与前端依赖）
if [[ "$REPO_DIR" != "$OPT_DIR" ]]; then
  # 覆盖之前留一份当前部署的快照，回滚就靠它（代码只有几 MB，不含 .venv 与 dist）
  if [[ -f "$OPT_DIR/backend/app/main.py" ]]; then
    SNAP="$RELEASE_DIR/$(date +%Y%m%d-%H%M%S).tar.gz"
    tar -C "$OPT_DIR" \
      --exclude='.venv' --exclude='./.venv' \
      --exclude='node_modules' --exclude='*/node_modules' \
      --exclude='./frontend/dist' --exclude='*/dist' \
      --exclude='./backend/data' --exclude='*/backend/data' \
      --exclude='__pycache__' --exclude='*/__pycache__' \
      -czf "$SNAP" .
    ls -1t "$RELEASE_DIR"/*.tar.gz 2>/dev/null | tail -n +6 | xargs -r rm -f
    echo "已备份当前部署：$SNAP（保留最近 5 份）"
  fi

  tar -C "$REPO_DIR" \
    --exclude='./.git' --exclude='*/.git' \
    --exclude='./frontend/node_modules' --exclude='*/node_modules' \
    --exclude='./frontend/dist' --exclude='*/dist' \
    --exclude='./backend/.venv' --exclude='*/.venv' \
    --exclude='./backend/data' --exclude='*/backend/data' \
    --exclude='./backend/__pycache__' --exclude='*/__pycache__' \
    -cf - . | tar -C "$OPT_DIR" -xf -
  chown -R "$APP_USER:$APP_USER" "$OPT_DIR"
fi

# 写一个版本标记，运维时不必猜"这台机器上跑的是哪一版"
if git -C "$REPO_DIR" describe --tags --always >/dev/null 2>&1; then
  git -C "$REPO_DIR" describe --tags --always > "$OPT_DIR/DEPLOYED_VERSION"
fi

# 环境变量文件：密钥运行时注入，仓库里不存
if [[ ! -f "$ENV_DIR/env" ]]; then
  cat > "$ENV_DIR/env" <<'ENVFILE'
# 运行时配置，改完 systemctl restart genai-mindmap 生效。权限 0640 root:genai。
# 这个文件是必需的：systemd 单元里没写 `-`，文件不在时服务会直接启动失败，
# 而不是带着空配置跑起来。
MINDMAP_LLM_MODE=model
MINDMAP_LLM_BASE_URL=
MINDMAP_LLM_API_KEY=
MINDMAP_LLM_MODEL=
MINDMAP_DB_PATH=/var/lib/genai-mindmap/mindmaps.db
DASHSCOPE_API_KEY=
MINDMAP_AGENT_ENABLED=1
MINDMAP_NLI_ENABLED=0
# docling 首次解析会去 HuggingFace 取版面模型；国内直连常超时，用镜像更快。
# 不需要就删掉这行。
HF_ENDPOINT=https://hf-mirror.com
ENVFILE
  echo "已生成 $ENV_DIR/env —— 记得填 MINDMAP_LLM_* 四项"
else
  echo "$ENV_DIR/env 已存在，保留不动"
fi
chown root:"$APP_USER" "$ENV_DIR/env"
chmod 640 "$ENV_DIR/env"

say "4/6 Python 与依赖（优先系统 3.12/3.11，uv 与清华 conda 镜像作退路）"
# 单独成脚本，方便装到一半失败时只重跑这一步：sudo bash deploy/setup-python.sh
OPT_DIR="$OPT_DIR" APP_USER="$APP_USER" bash "$REPO_DIR/deploy/setup-python.sh"

say "5/6 systemd"
install -m 644 "$REPO_DIR/deploy/genai-mindmap.service" /etc/systemd/system/genai-mindmap.service
systemctl daemon-reload
systemctl enable --now genai-mindmap

say "6/6 nginx"
mkdir -p /etc/nginx/conf.d /var/www/html

# IPv6 关掉时不能写 `listen [::]:80`：nginx 起不来，而 `nginx -t` 只查语法、查不出来。
LISTEN6=""
if [[ -r /proc/sys/net/ipv6/conf/all/disable_ipv6 ]] && [[ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6)" == "0" ]]; then
  LISTEN6="listen [::]:80;"
  echo "IPv6 可用，同时监听 v6"
else
  echo "IPv6 不可用（或已关闭），只监听 IPv4"
fi

# 已有证书就同时生成 443 块，并让 80 只做跳转；没有证书就只发 80。
if [[ -f "$CERT_DIR/fullchain.pem" ]]; then
  REDIRECT='return 301 https://$host$request_uri;'
  echo "检测到 $DOMAIN 的证书，配置 HTTPS"
else
  REDIRECT=""
  echo "还没有证书，只配 HTTP（之后加 --with-certbot 或手工 certbot certonly）"
fi

sed -e "s|__DOMAIN__|$DOMAIN|g" \
    -e "s|__WEB_ROOT__|$WEB_ROOT|g" \
    -e "s|__LISTEN6__|$LISTEN6|g" \
    -e "s|__REDIRECT__|$REDIRECT|g" \
  "$REPO_DIR/deploy/nginx.conf" > /etc/nginx/conf.d/genai-mindmap.conf

if [[ -f "$CERT_DIR/fullchain.pem" ]]; then
  sed -e "s|__DOMAIN__|$DOMAIN|g" \
      -e "s|__WEB_ROOT__|$WEB_ROOT|g" \
      -e "s|__CERT_DIR__|$CERT_DIR|g" \
      -e "s|__LISTEN6__|${LISTEN6/80/443}|g" \
    "$REPO_DIR/deploy/nginx-tls.conf" >> /etc/nginx/conf.d/genai-mindmap.conf
fi

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

# SELinux 默认不允许 nginx 反代到本地端口；AL3 默认 enforcing
setsebool -P httpd_can_network_connect 1 2>/dev/null || true

# 前端 dist 放在 /opt 下，SELinux 里那是 usr_t，nginx 读不到就是 403 ——
# 而且只报 403，日志里写 permission denied，看起来像文件权限没给够。
if command -v semanage >/dev/null 2>&1 && selinuxenabled 2>/dev/null; then
  semanage fcontext -a -t httpd_sys_content_t "$WEB_ROOT(/.*)?" 2>/dev/null \
    || semanage fcontext -m -t httpd_sys_content_t "$WEB_ROOT(/.*)?" 2>/dev/null \
    || true
  restorecon -R "$WEB_ROOT" 2>/dev/null || true
fi

nginx -t
systemctl enable --now nginx
# nginx -t 不真实 bind，端口冲突 / IPv6 关闭这类问题只会在启动时暴露
systemctl restart nginx

# 前端 dist 是 scp 上来的，默认权限可能是 600，nginx（用户 nginx）读不到就是 403
chmod -R a+rX "$WEB_ROOT" || true

# 备份：每天 03:20 用 sqlite 的在线备份接口导一份，保留 14 天
install -m 755 "$REPO_DIR/deploy/backup.sh" /usr/local/bin/genai-mindmap-backup
echo '20 3 * * * root /usr/local/bin/genai-mindmap-backup >/var/log/genai-mindmap/backup.log 2>&1' \
  > /etc/cron.d/genai-mindmap-backup
chmod 644 /etc/cron.d/genai-mindmap-backup

if [[ $WITH_CERTBOT -eq 1 ]]; then
  say "附带：certbot 签 HTTPS 证书"
  # AL3 的官方源里没有 certbot，走 EPEL
  dnf install -y epel-release || true
  dnf install -y certbot python3-certbot-nginx
  # 用 certonly --webroot：只签发，不动 nginx 配置。
  # 之前用 `certbot --nginx` 会把 443 写进 conf.d 里那个文件，而本脚本每次都按模板
  # 覆盖它，于是"重跑一次安装脚本，HTTPS 就没了"。现在证书与配置分开管理。
  certbot certonly --webroot -w /var/www/html -d "$DOMAIN" \
    --non-interactive --agree-tos --register-unsafely-without-email || {
      echo "证书签发失败：多半是域名还没解析到本机，或 80 端口没放行。修好后重跑：" >&2
      echo "  certbot certonly --webroot -w /var/www/html -d $DOMAIN" >&2
    }
  systemctl enable --now certbot-renew.timer 2>/dev/null || true
  echo "证书就绪后重跑本脚本（不带参数也行），会把 443 块写进 nginx 配置并让 80 跳转"
fi

printf '\n完成。还差三件事：\n'
printf '  1. 填 %s（MINDMAP_LLM_BASE_URL / API_KEY / MODEL），然后 systemctl restart genai-mindmap\n' "$ENV_DIR/env"
printf '  2. 本机 npm run build 后把 frontend/dist 传到 %s，然后 chmod -R a+rX 并 restorecon -R\n' "$WEB_ROOT"
printf '  3. 域名解析到本机后签证书：certbot certonly --webroot -w /var/www/html -d %s，再重跑本脚本\n' "$DOMAIN"
printf '\n自检：curl -u demo:**** http://127.0.0.1/api/health\n'
printf '日志：journalctl -u genai-mindmap -n 50 --no-pager；systemctl status nginx\n'
