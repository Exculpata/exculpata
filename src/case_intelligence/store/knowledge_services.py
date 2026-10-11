"""Compose human-knowledge services on the store's connection and transaction lock.

Existing store entry points remain inherited; no new connection or commit owner.
"""


class KnowledgeServicesMixin:
    def _matter_purpose_service(self, *, administrator_override=False):
        from ..matter_purpose import MatterPurposeService
        from ..matter_purpose_repository import MatterPurposeRepository
        unit = self.entity_repository(export_read=administrator_override,
                                      administrator_override=administrator_override)
        return MatterPurposeService(MatterPurposeRepository(unit, self._append_audit_event_locked))

    def matter_purpose(self, matter_id, actor_id, *, administrator_override=False,
                       check_authority=lambda: None):
        return self._matter_purpose_service(administrator_override=administrator_override).read(
            matter_id, actor_id, check_authority=check_authority)

    def update_matter_purpose(self, matter_id, actor_id, *, expected_revision, objective,
                              background, request_id, session_id=None, check_authority=lambda: None):
        return self._matter_purpose_service().update(matter_id, actor_id,
            expected_revision=expected_revision, objective=objective, background=background,
            request_id=request_id, session_id=session_id, check_authority=check_authority)

    def entity_repository(self, *, export_read=False, administrator_override=False, automatic=False):
        from ..entity_repository import EntityRepository
        authority = (lambda matter_id, actor_id: self._authorize_export_read(
            matter_id, actor_id, administrator_override=administrator_override)) if export_read else self.membership
        read_authority = None
        if automatic:
            authority = self._authorize_automatic_discovery
            read_authority = lambda matter_id, actor_id: self._authorize_automatic_discovery(
                matter_id, actor_id, create=False)
        return EntityRepository(connection=self.connection, lock=self._lock,
                                authorize=authority, now=self._now, read_authorize=read_authority)

    def assertion_repository(self, *, export_read=False, administrator_override=False):
        from ..assertion_repository import AssertionRepository
        return AssertionRepository(self.entity_repository(
            export_read=export_read, administrator_override=administrator_override))

