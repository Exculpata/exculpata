"""Matter-scoped case theory repository contract (implementation follows tests)."""


class CaseTheoryError(ValueError):
    pass


class CaseTheoryRepository:
    def __init__(self, connection):
        self.connection = connection

    def create(self, matter_id, claims, actor_id, *, assertion_matter=None):
        raise NotImplementedError

    def read(self, matter_id, version=None):
        raise NotImplementedError

    def export(self, matter_id):
        raise NotImplementedError


class CaseTheoryService:
    def __init__(self, repository, *, authorize, assertion_matter):
        self.repository = repository
        self.authorize = authorize
        self.assertion_matter = assertion_matter

    def create_version(self, matter_id, claims, actor_id="case-team"):
        return self.repository.create(matter_id, claims, actor_id,
                                      assertion_matter=self.assertion_matter)

    def current(self, matter_id):
        return self.repository.read(matter_id)

    def version(self, matter_id, version):
        return self.repository.read(matter_id, version)

    def list_claims(self, matter_id, version=None):
        return self.repository.read(matter_id, version)["claims"]

    def export(self, matter_id):
        return self.repository.export(matter_id)

    def for_report(self, matter_id):
        return self.current(matter_id)
