#!/usr/bin/env bash
set -Eeuo pipefail
echo "Deprecation notice: recordbench-backup.sh is deprecated; use exculpata-backup.sh." >&2
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/exculpata_backup.py" backup "$@"
