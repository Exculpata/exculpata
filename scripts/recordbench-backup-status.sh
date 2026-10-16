#!/usr/bin/env bash
set -Eeuo pipefail
echo "Deprecation notice: recordbench-backup-status.sh is deprecated; use exculpata-backup-status.sh." >&2
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/exculpata_backup.py" status "$@"
