"""Read-only access to the durable media queue during stopped-node maintenance."""
from contextlib import closing
from pathlib import Path
import sqlite3


ACTIVE_MEDIA_JOB = "state IN ('queued','running')"


def has_active_media_jobs(path: Path) -> bool:
    """Inspect an existing control store without initializing or upgrading it."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute(
            "SELECT 1 FROM workbench_media_job WHERE " + ACTIVE_MEDIA_JOB + " LIMIT 1"
        ).fetchone() is not None
