#!/usr/bin/env bash
# 准备 Python 运行时与后端依赖。
#
# 为什么不是"一条 uv 命令搞定"：国内 ECS 上 astral.sh 与 GitHub 经常连不上
# （实测 curl: (35) OpenSSL SSL_connect），而系统自带的 python3 又太旧（3.6/3.8）。
# 所以按四级退路依次尝试，谁先成谁算：
#   1. 系统里已有 python3.12 / 3.11 / 3.10
#   2. dnf 装 python3.12 或 python3.11（Alinux 3 的 appstream 里有）
#   3. uv（只在能连上 astral.sh 时用得上）
#   4. 清华镜像的 Miniconda（兜底，最慢但基本一定成）
set -euo pipefail

OPT_DIR="${OPT_DIR:-/opt/genai-mindmap}"
APP_USER="${APP_USER:-genai}"
VENV="$OPT_DIR/.venv"
PIP_INDEX="${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple}"
CONDA_MIRROR="${CONDA_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\n[失败] %s\n' "$*" >&2; exit 1; }

# docling 2.x 要求 >= 3.9，这里卡 3.10 更稳（类型语法、asyncio 行为都对齐 CI 的 3.12）
version_ok() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null
}

find_python() {
  local cand
  for cand in python3.12 python3.11 python3.10; do
    if command -v "$cand" >/dev/null 2>&1 && version_ok "$cand"; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

PYTHON="$(find_python || true)"

if [[ -z "$PYTHON" ]]; then
  log "系统里没有 3.10+，尝试用 dnf 装 python3.12 / python3.11"
  dnf install -y python3.12 python3.12-pip python3.12-devel >/dev/null 2>&1 || true
  PYTHON="$(find_python || true)"
fi

if [[ -z "$PYTHON" ]]; then
  dnf install -y python3.11 python3.11-pip python3.11-devel >/dev/null 2>&1 || true
  PYTHON="$(find_python || true)"
fi

if [[ -z "$PYTHON" ]]; then
  log "dnf 没有可用的新版 Python，改试 uv"
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf --max-time 60 https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh || true
  fi
  if command -v uv >/dev/null 2>&1 || [[ -x /usr/local/bin/uv ]]; then
    UV_BIN="$(command -v uv || echo /usr/local/bin/uv)"
    # python-build-standalone 走 GitHub，直连不通时用镜像前缀
    sudo -u "$APP_USER" env HOME="$OPT_DIR" \
      UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-https://ghfast.top/https://github.com/astral-sh/python-build-standalone/releases/download}" \
      "$UV_BIN" python install 3.12 || true
    if sudo -u "$APP_USER" env HOME="$OPT_DIR" "$UV_BIN" venv --python 3.12 "$VENV" 2>/dev/null; then
      log "uv 装好了 Python 与虚拟环境"
    fi
  fi
fi

if [[ ! -x "$VENV/bin/python" && -z "$PYTHON" ]]; then
  log "改用清华镜像的 Miniconda（最后一条退路，下载约 100MB）"
  curl -fL --max-time 600 -o /tmp/miniconda.sh "$CONDA_MIRROR/Miniconda3-latest-Linux-x86_64.sh" \
    || die "Miniconda 下载失败，请检查网络或手动准备 Python 3.10+"
  bash /tmp/miniconda.sh -b -p /opt/miniconda3 >/dev/null
  /opt/miniconda3/bin/conda create -y -p "$VENV" python=3.12 pip >/dev/null
  rm -f /tmp/miniconda.sh
  log "Miniconda 已建好 $VENV"
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  [[ -n "$PYTHON" ]] || die "没找到可用的 Python 3.10+"
  log "用 $PYTHON 建虚拟环境：$VENV"
  rm -rf "$VENV"
  sudo -u "$APP_USER" "$PYTHON" -m venv "$VENV" \
    || die "$PYTHON 缺少 venv 模块，先 dnf install -y $PYTHON-devel python3-pip"
fi

log "安装后端依赖（PyPI 走 ${PIP_INDEX}）"
sudo -u "$APP_USER" "$VENV/bin/python" -m pip install --upgrade pip -i "$PIP_INDEX" >/dev/null
sudo -u "$APP_USER" "$VENV/bin/python" -m pip install -i "$PIP_INDEX" -r "$OPT_DIR/backend/requirements.txt"

"$VENV/bin/python" -V
echo "Python 依赖就绪：$VENV"
