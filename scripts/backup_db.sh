#!/usr/bin/env bash
# scripts/backup_db.sh — Nightly rotating 7-day PostgreSQL backup script.

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/sewasetu}"
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/sewasetu_${TIMESTAMP}.sql.gz"
CONTAINER_NAME="sewasetu-assistant-db-1"

mkdir -p "$BACKUP_DIR"

echo "[$TIMESTAMP] Starting database backup from container ${CONTAINER_NAME}..."

# Execute pg_dump inside container and compress with gzip
docker exec "$CONTAINER_NAME" pg_dump -U sewasetu sewasetu | gzip > "$BACKUP_FILE"

FILE_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[$TIMESTAMP] Backup completed: ${BACKUP_FILE} (${FILE_SIZE})"

# Retain only the last 7 days of backups
echo "[$TIMESTAMP] Pruning backups older than 7 days in ${BACKUP_DIR}..."
find "$BACKUP_DIR" -type f -name "sewasetu_*.sql.gz" -mtime +7 -delete

echo "[$TIMESTAMP] Backup rotation complete."
