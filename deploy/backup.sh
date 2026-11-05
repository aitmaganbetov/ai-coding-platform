#!/usr/bin/env bash
# Back up the server database (ТЗ §15: backups are mandatory in multi-user mode).
# Usage: deploy/backup.sh [BACKUP_DIR] [KEEP_DAYS]   (run from cron, e.g. hourly/daily)
# Restore: gunzip -c FILE | docker compose -f deploy/docker-compose.yml exec -T postgres \
#            psql -U aicoding -d aicoding
set -euo pipefail
cd "$(dirname "$0")"
dir="${1:-./backups}"
keep="${2:-14}"
mkdir -p "$dir"
chmod 700 "$dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$dir/aicoding-$stamp.sql.gz"
umask 077
docker compose exec -T postgres pg_dump -U aicoding -d aicoding --no-owner --clean --if-exists \
  | gzip -9 > "$out.partial"
mv "$out.partial" "$out"
find "$dir" -name 'aicoding-*.sql.gz' -mtime +"$keep" -delete
echo "backup written: $out"
