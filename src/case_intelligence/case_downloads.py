"""Server-observed archive byte coverage, never a promise of a durable backup."""
import json
import uuid

from .store_records import WorkspaceProblem


class CaseDownloads:
    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.workspace = lifecycle.workspace
        self.connection = lifecycle.connection

    def begin(self, matter_id, actor_id, session_id, export_id, archive_digest,
              total_size, start, end, *, administrator_override=False):
        """Called only by verified archive transport; end is exclusive."""
        if (any(type(n) is not int for n in (total_size, start, end))
                or not 0 <= start < end <= total_size):
            raise ValueError('Invalid archive delivery interval.')
        with self.lifecycle._transaction():
            self.lifecycle._owner(matter_id, actor_id, administrator_override)
            self.lifecycle._session(actor_id, session_id, administrator_override)
            current = self.lifecycle.current_exit(matter_id)
            if (current is None or current.state != 'closing' or current.mode != 'export_exit'
                    or current.export_id != export_id or current.archive_digest != archive_digest
                    or self.workspace.matter_lifecycle(matter_id).state != 'closing'):
                raise WorkspaceProblem('The export changed; download its current archive.')
            attempt_id = 'archive-download-' + uuid.uuid4().hex
            self.connection.execute(
                'INSERT INTO workbench_case_download_attempt VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (attempt_id, current.exit_id, export_id, archive_digest, total_size, actor_id,
                 session_id, int(administrator_override), start, end, self.workspace._now()))
            return attempt_id

    def discard(self, attempt_id):
        with self.lifecycle._transaction():
            self.connection.execute('DELETE FROM workbench_case_download_attempt WHERE attempt_id=?',
                                    (attempt_id,))

    def complete(self, attempt_id):
        """Transport calls only after all requested bytes reach the outer send."""
        with self.lifecycle._transaction():
            attempt = self.connection.execute(
                'SELECT * FROM workbench_case_download_attempt WHERE attempt_id=?', (attempt_id,)).fetchone()
            if attempt is None:
                return False  # Cancel/replacement/restart invalidates late callbacks.
            current = self.connection.execute('SELECT * FROM workbench_case_exit WHERE exit_id=?',
                                               (attempt['exit_id'],)).fetchone()
            matter_id = current['matter_id']
            actor_id = attempt['actor_id']
            administrator = bool(attempt['administrator'])
            self.lifecycle._owner(matter_id, actor_id, administrator)
            self.lifecycle._session(actor_id, attempt['session_id'], administrator)
            if (current['state'] != 'closing' or current['mode'] != 'export_exit'
                    or current['export_id'] != attempt['export_id']
                    or current['archive_digest'] != attempt['archive_digest']
                    or self.workspace.matter_lifecycle(matter_id).state != 'closing'):
                raise WorkspaceProblem('The export changed during delivery.')
            key = (attempt['exit_id'], attempt['export_id'], attempt['archive_digest'], actor_id)
            previous = self.connection.execute(
                'SELECT * FROM workbench_case_download_coverage WHERE exit_id=? AND export_id=? '
                'AND archive_digest=? AND actor_id=?', key).fetchone()
            if previous is not None and previous['total_size'] != attempt['total_size']:
                raise WorkspaceProblem('The archive size changed during delivery.')
            intervals = json.loads(previous['intervals_json']) if previous else []
            intervals.append([attempt['range_start'], attempt['range_end']])
            merged = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(end, merged[-1][1])
                else:
                    merged.append([start, end])
            # Bound hostile fragmentation. A full subsequent download still succeeds.
            if len(merged) > 1024:
                self.connection.execute('DELETE FROM workbench_case_download_attempt WHERE attempt_id=?',
                                        (attempt_id,))
                return False
            completed = merged == [[0, attempt['total_size']]]
            completed_at = ((previous['completed_at'] if previous else None) or self.workspace._now()) if completed else None
            self.connection.execute(
                'INSERT INTO workbench_case_download_coverage VALUES (?,?,?,?,?,?,?) '
                'ON CONFLICT(exit_id,export_id,archive_digest,actor_id) DO UPDATE SET '
                'intervals_json=excluded.intervals_json,completed_at=excluded.completed_at',
                (*key[:3], attempt['total_size'], actor_id, json.dumps(merged), completed_at))
            self.connection.execute('DELETE FROM workbench_case_download_attempt WHERE attempt_id=?',
                                    (attempt_id,))
            if completed and (previous is None or previous['completed_at'] is None):
                self.lifecycle._audit('matter.exit_download', matter_id, actor_id,
                                      attempt['exit_id'], attempt['session_id'],
                                      administrator_override=administrator)
            return completed

    def visible_note(self, model_notice=""):
        notice = str(model_notice or "")
        return notice

    def receipt(self, current, actor_id):
        with self.workspace._lock:
            return self.connection.execute(
                'SELECT * FROM workbench_case_download_coverage WHERE exit_id=? AND export_id=? '
                'AND archive_digest=? AND actor_id=? AND completed_at IS NOT NULL',
                (current.exit_id, current.export_id, current.archive_digest, actor_id)).fetchone()

    def invalidate(self, exit_id):
        """Caller holds the lifecycle transaction."""
        self.connection.execute('DELETE FROM workbench_case_download_attempt WHERE exit_id=?', (exit_id,))
        self.connection.execute('DELETE FROM workbench_case_download_coverage WHERE exit_id=?', (exit_id,))
