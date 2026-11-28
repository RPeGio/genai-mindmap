#!/usr/bin/env bash
# 在线备份 PostgreSQL 数据库。由 /etc/cron.d/genai-mindmap-backup 每天调用，
# 也可以手动跑：sudo /usr/local/bin/genai-mindmap-backup
#
# 用 pg_dump 而不是直接复制数据目录：服务在跑的时候 cp PG 的 data 目录，拿到的
# 是一份**撕裂的集群**（各后台进程分别写了一部分页、WAL 与数据文件对不上），
# 要恢复时才发现起不来。pg_dump 在服务端开一个一致性快照，导出的是一份"某一时刻"
# 的库。`-Fc`（custom 格式）自带压缩，并且能用 `pg_restore -t 表名` 只捞一张表出来
# ——所以既不要再 gzip，也别换成纯 SQL 文本（体积大、且不能挑表恢复）。
#
# 恢复：pg_restore -d <空库> mindmaps-<时间戳>.dump（步骤见 docs/DEPLOY.md）
set -euo pipefail

# cron 不继承 systemd 的 EnvironmentFile。不自己读一遍的话，运维把 MINDMAP_PG_DSN
# 改到别处（比如另起一台 PG）之后，这里仍然用下面的默认值——而下面"连不上"的分支
# 在没配过路径时只留一行提示就退出，结果是每天"成功"地零备份，直到要恢复才发现。
# `set -a` 让 source 进来的变量能传给 pg_dump / pg_isready 子进程。
if [[ -r /etc/genai-mindmap/env ]]; then
  set -a
  # shellcheck disable=SC1091
  . /etc/genai-mindmap/env
  set +a
fi

# 默认值与 app/db/pool.py 的 DEFAULT_DSN 一致（本机 unix socket 上的 mindmaps 库）。
# install.sh 会把真实连接串显式写进 /etc/genai-mindmap/env，正常路径上走的是那一条。
#
# **注意认证身份**：本脚本由 cron 以 root 跑，DSN 不带 user 时身份就是运行者（= root）。
# peer / scram 认证下 root 通常没有对应 role，pg_dump 会直接报认证失败。所以给
# MINDMAP_PG_DSN 时要带上能登录的 user（服务端要求口令时连口令一起）——不要再按
# SQLite 时代的直觉以为"root 什么都读得到"。
DSN="${MINDMAP_PG_DSN:-postgresql:///mindmaps?host=/var/run/postgresql}"
DEST="${MINDMAP_BACKUP_DIR:-/var/backups/genai-mindmap}"
KEEP_DAYS="${MINDMAP_BACKUP_KEEP_DAYS:-14}"

# 区分两件事：显式配了连接串却连不上 = 配错了，要报错让 cron 日志留下痕迹；
# 没配过（用默认值）且连不上 = 这台机器还没装/还没起 PG，跳过即可。
# 用 pg_isready 而不是直接让 pg_dump 失败：后者的 stderr 里混着认证失败、库不存在、
# 客户端版本比服务端旧等好几种原因，而这里要判断的只是"该不该报错退出"这一件事。
if ! pg_isready -q --dbname="$DSN"; then
  if [[ -n "${MINDMAP_PG_DSN:-}" ]]; then
    echo "$(date '+%F %T') 连不上 MINDMAP_PG_DSN 指向的 PostgreSQL，备份未执行" >&2
    exit 1
  fi
  echo "$(date '+%F %T') 连不上默认的 PostgreSQL，跳过（还没装/还没起库？）"
  exit 0
fi

mkdir -p "$DEST"
STAMP="$(date '+%Y%m%d-%H%M%S')"
OUT="$DEST/mindmaps-$STAMP.dump"

# 失败时把半截文件删掉：`-f` 留下的截断 dump 比没有更糟——被保留策略当成一份
# "正常备份"留十几天，等真要恢复时才发现它是废的。
if ! pg_dump --dbname="$DSN" -Fc -f "$OUT"; then
  rm -f "$OUT"
  echo "$(date '+%F %T') pg_dump 失败，未留下备份文件" >&2
  exit 1
fi

# 保留最近 N 天；按修改时间删，避免依赖文件名里的时间戳格式
find "$DEST" -name 'mindmaps-*.dump' -type f -mtime "+$KEEP_DAYS" -delete

# 不打印 DSN：它可能带口令，而这里的输出归 cron 写进 /var/log/genai-mindmap/backup.log。
echo "$(date '+%F %T') 已备份 PostgreSQL -> ${OUT}（保留 ${KEEP_DAYS} 天）"
