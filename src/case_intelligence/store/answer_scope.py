"""Durable answer scope and the read-only basis for an explicit investigation."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerInvestigationBasis:
    question: str
    source_set_id: str
    scope_available: bool
    can_query: bool
    has_active_work: bool


class AnswerScopeMixin:
    def answer_source_set_id(self, matter_id: str, job_id: str) -> str | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT source_set_id FROM workbench_answer_source_scope "
                "WHERE matter_id=? AND job_id=?",
                (matter_id, job_id),
            ).fetchone()
        return row["source_set_id"] if row is not None else None

    def answer_investigation_basis(
        self, matter_id: str, actor_id: str, conversation_id: str, message_id: str,
    ) -> AnswerInvestigationBasis | None:
        """Read the original question and current availability for a member.

        The completed job binds the answer to its question even when jobs finish
        out of order. This is presentation state only: submission must still use
        the existing research admission checks and idempotency boundary.
        """
        with self._lock:
            self.membership(matter_id, actor_id)
            if not any(item.conversation_id == conversation_id
                       for item in self.conversations(matter_id, include_archived=False)):
                raise KeyError(conversation_id)
            row = self.connection.execute(
                "SELECT j.question,s.source_set_id FROM workbench_answer_job j "
                "JOIN workbench_message q ON q.message_id=j.question_message_id "
                "AND q.conversation_id=j.conversation_id AND q.role='user' AND q.content=j.question "
                "LEFT JOIN workbench_answer_source_scope s ON s.job_id=j.job_id AND s.matter_id=j.matter_id "
                "WHERE j.matter_id=? AND j.conversation_id=? AND j.result_message_id=? AND j.state='succeeded'",
                (matter_id, conversation_id, message_id),
            ).fetchone()
            if row is None or not row["question"] or len(row["question"]) > 2_000:
                return None
            scope = row["source_set_id"] or ""
            scope_available = True
            if scope:
                try:
                    scope_available = self.source_set(matter_id, scope).source_count > 0
                except KeyError:
                    scope_available = False
            active = self.connection.execute(
                "SELECT 1 FROM workbench_answer_job WHERE matter_id=? AND conversation_id=? AND state IN ('queued','running') "
                "UNION ALL SELECT 1 FROM workbench_research_job WHERE matter_id=? AND conversation_id=? AND state IN ('queued','running') LIMIT 1",
                (matter_id, conversation_id, matter_id, conversation_id),
            ).fetchone()
            return AnswerInvestigationBasis(
                question=row["question"], source_set_id=scope,
                scope_available=scope_available,
                can_query=self.matter_readiness(matter_id).can_query,
                has_active_work=active is not None,
            )
