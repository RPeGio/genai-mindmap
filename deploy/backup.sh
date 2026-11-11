#!/usr/bin/env bash
# 在线备份 SQLite 数据文件。由 /etc/cron.d/genai-mindmap-backup 每天调用，
# 也可以手动跑：sudo /usr/local/bin/genai-mindmap-backup
#
# 用 sqlite 的 .backup 命令而不是 cp：服务在跑的时候直接复制文件，
# 有可能拿到"页写了一半"的副本（WAL / 未落盘页），恢复时才发现库损坏。
set -euo pipefail

DB="${MINDMAP_DB_PATH:-/var/lib/genai-mindmap/mindmaps.db}"
DEST="${MINDMAP_BACKUP_DIR:-/var/backups/genai-mindmap}"
KEEP_DAYS="${MINDMAP_BACKUP_KEEP_DAYS:-14}"

if [[ ! -f "$DB" ]]; then
  echo "$(date '+%F %T') 没有数据库文件：$DB，跳过"
  exit 0
fi

mkdir -p "$DEST"
STAMP="$(date '+%Y%m%d-%H%M%S')"
OUT="$DEST/mindmaps-$STAMP.db"

sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"

# 保留最近 N 天；按修改时间删，避免依赖文件名里的时间戳格式
find "$DEST" -name 'mindmaps-*.db.gz' -type f -mtime "+$KEEP_DAYS" -delete

echo "$(date '+%F %T') 已备份 ${DB} -> ${OUT}.gz（保留 ${KEEP_DAYS} 天）"
