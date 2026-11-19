"""Transactional grants for routine external model dispatch."""
from uuid import uuid4

from ..external_model_grants import CAPABILITIES, ModelDestination, configured_model_destination
from ..store_records import WorkspaceProblem


class ModelGrantStoreMixin:
    @staticmethod
    def _configured_grant_destination(capability):
        try:
            return configured_model_destination(capability)
        except ValueError:
            return None

    def _observe_model_destination_locked(self, capability):
        destination = self._configured_grant_destination(capability)
        endpoint, model = (destination.endpoint, destination.model) if destination else ("", "")
        observed = self.connection.execute(
            "SELECT endpoint,model,configuration_id FROM workbench_model_destination WHERE capability=?", (capability,),
        ).fetchone()
        if observed is not None and (observed["endpoint"], observed["model"]) == (endpoint, model):
            return destination, observed["configuration_id"]
        previous_id = observed["configuration_id"] if observed else None
        for row in self.connection.execute(
            "SELECT grant_id,matter_id FROM workbench_matter_model_grant "
            "WHERE capability=? AND configuration_id=? AND approved=1", (capability, previous_id),
        ):
            self._append_audit_event_locked(
                actor_principal_id=None, session_id=None, matter_id=row["matter_id"], request_id="model-config",
                action="model_grant.expire", outcome="success", object_type=f"model_grant.{capability}",
                object_id=row["grant_id"],
            )
        configuration_id = uuid4().hex
        self.connection.execute(
            "INSERT INTO workbench_model_destination(capability,endpoint,model,configuration_id) VALUES (?,?,?,?) "
            "ON CONFLICT(capability) DO UPDATE SET endpoint=excluded.endpoint, model=excluded.model, "
            "configuration_id=excluded.configuration_id", (capability, endpoint, model, configuration_id),
        )
        return destination, configuration_id

    def observe_model_destinations(self) -> None:
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            for capability in CAPABILITIES:
                self._observe_model_destination_locked(capability)

    def model_grant_allows(self, matter_id: str, destination: ModelDestination) -> bool:
        if not isinstance(destination, ModelDestination):
            return False
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current, configuration_id = self._observe_model_destination_locked(destination.capability)
            if current != destination:
                return False
            return self._model_grant_active_locked(matter_id, destination, configuration_id)

    def _model_grant_active_locked(self, matter_id, destination, configuration_id):
        return self.connection.execute(
            "SELECT 1 FROM workbench_matter_model_grant g JOIN workbench_matter_lifecycle l USING(matter_id) "
            "WHERE matter_id=? AND capability=? AND endpoint=? AND model=? "
            "AND configuration_id=? AND approved=1 AND l.state='active'",
            (matter_id, destination.capability, destination.endpoint, destination.model, configuration_id),
        ).fetchone() is not None

    def model_grant_review(self, matter_id: str) -> tuple[tuple[ModelDestination, bool, str], ...]:
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = []
            for capability in CAPABILITIES:
                destination, configuration_id = self._observe_model_destination_locked(capability)
                if destination is not None:
                    approved = self._model_grant_active_locked(matter_id, destination, configuration_id)
                    rows.append((destination, approved, configuration_id))
            return tuple(rows)

    def model_grants(self, matter_id: str) -> tuple[ModelDestination, ...]:
        return tuple(destination for destination, approved, _ in self.model_grant_review(matter_id) if approved)

    def set_model_grant(self, matter_id: str, destination: ModelDestination, actor_id: str, *,
                        approved: bool, administrator_override: bool = False, session_id: str | None = None,
                        request_id: str = "model-grant", check_authority=None,
                        expected_configuration_id: str | None = None) -> None:
        if type(approved) is not bool or type(administrator_override) is not bool or not isinstance(destination, ModelDestination):
            raise WorkspaceProblem("Invalid model grant.")
        key = matter_id, destination.capability, destination.endpoint, destination.model
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if administrator_override and check_authority is None:
                raise WorkspaceProblem("Changing model approval as an administrator requires a current authority check.")
            if check_authority is not None:
                check_authority()
            actor = self.get_principal(actor_id)
            if not actor.active or not self.principal_enabled(actor.provider, actor.provider_subject):
                raise WorkspaceProblem("An active operator is required to change model approval.")
            if not administrator_override and self.membership(matter_id, actor_id).role != "owner":
                raise WorkspaceProblem("Only a matter owner or administrator can change model approval.")
            self._active_matter_locked(matter_id)
            configured, configuration_id = self._observe_model_destination_locked(destination.capability)
            reviewed = expected_configuration_id is None or expected_configuration_id == configuration_id
            if reviewed and (not approved or configured == destination):
                self._write_model_grant_locked(key, approved, configuration_id, actor_id, request_id, session_id)
                return
        # A rejected stale approval must still commit the observed rotation.
        raise WorkspaceProblem("The model configuration changed. Review the current destination.")

    def _write_model_grant_locked(self, key, approved, configuration_id, actor_id, request_id, session_id):
        matter_id, capability, _, _ = key
        row = self.connection.execute(
            "SELECT grant_id,approved,configuration_id FROM workbench_matter_model_grant "
            "WHERE matter_id=? AND capability=? AND endpoint=? AND model=?", key,
        ).fetchone()
        if bool(row and row["approved"] and row["configuration_id"] == configuration_id) == approved:
            return
        grant_id = row["grant_id"] if row else uuid4().hex
        self.connection.execute(
            "INSERT INTO workbench_matter_model_grant(grant_id,matter_id,capability,endpoint,model,approved,configuration_id) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(matter_id,capability,endpoint,model) "
            "DO UPDATE SET approved=excluded.approved,configuration_id=excluded.configuration_id",
            (grant_id, *key, approved, configuration_id),
        )
        self._append_audit_event_locked(
            actor_principal_id=actor_id, session_id=session_id, matter_id=matter_id,
            request_id=request_id, action="model_grant.approve" if approved else "model_grant.revoke",
            outcome="success", object_type=f"model_grant.{capability}", object_id=grant_id,
        )
