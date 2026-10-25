"""Fail-closed comparison of attributed accounts attached to evidence assertions."""

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from itertools import combinations, groupby
import re

from .workspace_store import WorkspaceProblem

LABEL = "possible inconsistency for review"
MAX_CANDIDATES = 500
_REVIEW_STATES = frozenset(("unreviewed", "reviewed", "dismissed"))
_TIME = re.compile(
    r"\b(?:(noon|midnight)|([01]?\d|2[0-3])"
    r"(?::([0-5]\d)(?:\s*([ap])\.?m\.?)?|\s*([ap])\.?m\.?))(?!\w)", re.I)
_QUANTITY = re.compile(r"\b\d+(?:\.\d+)?\b")
_PLACE = re.compile(
    r"\b(?:beside|inside|outside|near|at)\s+"
    r"(?!\d|(?:the\s+)?(?:time|noon|midnight|least|once)\b)(?:the\s+)?"
    r"([a-z][a-z -]{2,40}?)(?=[.,;]|\s+(?:beside|inside|outside|near|at|on|in|by|"
    r"before|after|under|over|from|to|with)\b|$)", re.I)


@dataclass(frozen=True)
class CitedPassage:
    account_id: str
    document_id: str
    source_version_id: str
    source_name: str
    location: str
    excerpt: str
    support_token: str
    attributed_to: str


@dataclass(frozen=True)
class InconsistencyCandidate:
    candidate_id: str
    label: str
    note: str
    first: CitedPassage
    second: CitedPassage
    comparison: str
    review_state: str = "unreviewed"


class CandidateResults(tuple):
    """Tuple-compatible cited leads, with an exact count omitted by the display cap."""

    def __new__(cls, candidates, not_shown):
        result = super().__new__(cls, candidates)
        result.not_shown = not_shown
        return result


def _normalized(value):
    return " ".join(value.casefold().split())


def _time_minutes(text):
    result = set()
    for named, hours, minutes, marker, hour_marker in _TIME.findall(text):
        if named:
            result.add(720 if named.casefold() == "noon" else 0)
            continue
        hour, marker = int(hours), marker or hour_marker
        if marker:
            hour = hour % 12 + (12 if marker.casefold() == "p" else 0)
        result.add(hour * 60 + int(minutes or "0"))
    return result


def _differences(first, second):
    """Return only explicit, bounded surface differences; make no truth finding.

    A one-sided place superset is intentionally ignored: extra location detail
    alone need not contradict the shared place.
    """
    left_times, right_times = _time_minutes(first), _time_minutes(second)
    if left_times and right_times and left_times != right_times:
        return "time"
    left_places = {_normalized(value) for value in _PLACE.findall(first)} - {"end", "night"}
    right_places = {_normalized(value) for value in _PLACE.findall(second)} - {"end", "night"}
    if left_places - right_places and right_places - left_places:
        return "place"
    # Times are a kind of number and have already received the more useful label.
    left_numbers = set(_QUANTITY.findall(_TIME.sub("", first)))
    right_numbers = set(_QUANTITY.findall(_TIME.sub("", second)))
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return "quantity"
    return ""


class WitnessInconsistencyService:
    """Find review leads without preserving generated text or inferring credibility."""

    def __init__(self, assertion_repository, entity_service):
        self.repository = assertion_repository
        self.entities = entity_service

    @staticmethod
    def _citation(account):
        required = ("account_id", "document_id", "source_version_id", "source_name",
                    "location", "excerpt", "support_token", "attributed_to")
        if any(not isinstance(account.get(key), str) or not account[key].strip() for key in required):
            return None
        return CitedPassage(**{key: account[key] for key in required})

    @staticmethod
    def _candidate(matter_id, first, second, comparison, difference):
        ordered = [asdict(WitnessInconsistencyService._citation(account))
                   for account in sorted((first, second), key=lambda row: row["account_id"])]
        identity = json.dumps([matter_id, difference, ordered], sort_keys=True, separators=(",", ":"))
        candidate_id = "witness-inconsistency-" + hashlib.sha256(identity.encode()).hexdigest()
        return InconsistencyCandidate(candidate_id, LABEL,
            f"The cited passages differ in stated {difference}.",
            WitnessInconsistencyService._citation(first),
            WitnessInconsistencyService._citation(second), comparison)

    def find(self, matter_id, actor_id):
        with self.entities.source_guard(), self.repository.reading(matter_id, actor_id) as repo:
            return self._find_current(repo, matter_id)

    def list_dismissed(self, matter_id, actor_id):
        with self.entities.source_guard(), self.repository.reading(matter_id, actor_id) as repo:
            return self._find_current(repo, matter_id, dismissed=True)

    def _accounts(self, repo, matter_id, account_ids=()):
        selection = " AND a.account_id IN (?,?)" if account_ids else ""
        rows = [dict(row) for row in repo.connection.execute(
            "SELECT a.* FROM workbench_assertion_account a "
            "JOIN workbench_assertion r ON r.matter_id=a.matter_id AND r.assertion_id=a.assertion_id "
            "WHERE a.matter_id=? AND r.record_type='event'" + selection +
            " ORDER BY a.assertion_id,a.account_id", (matter_id, *account_ids))]
        valid = self.entities.validate_references(rows)
        return [row for index, row in enumerate(rows)
                if index in valid and self._citation(row) is not None]

    def _candidate_pairs(self, repo, matter_id):
        for _, accounts in groupby(self._accounts(repo, matter_id), key=lambda row: row["assertion_id"]):
            for first, second in combinations(accounts, 2):
                same_witness = _normalized(first["attributed_to"]) == _normalized(second["attributed_to"])
                difference = _differences(first["excerpt"], second["excerpt"])
                if difference:
                    comparison = "same witness" if same_witness else "different witnesses on the same event"
                    yield self._candidate(matter_id, first, second, comparison, difference), [first, second]

    def _find_current(self, repo, matter_id, *, dismissed=False):
        states = {row["candidate_id"]: row["state"] for row in repo.connection.execute(
            "SELECT candidate_id,state FROM workbench_witness_inconsistency_review WHERE matter_id=?",
            (matter_id,))}
        results, not_shown = [], 0
        for candidate, _ in self._candidate_pairs(repo, matter_id):
            state = states.get(candidate.candidate_id, "unreviewed")
            if (state == "dismissed") == dismissed:
                if len(results) < MAX_CANDIDATES:
                    results.append(replace(candidate, review_state=state))
                else:
                    not_shown += 1
        return CandidateResults(results, not_shown)

    def _set_state(self, matter_id, actor_id, candidate_id, state):
        if (state not in _REVIEW_STATES or not isinstance(candidate_id, str)
                or not re.fullmatch(r"witness-inconsistency-[0-9a-f]{64}", candidate_id)):
            raise WorkspaceProblem("Choose a current inconsistency candidate and review state.")
        with self.entities.source_guard():
            with self.repository.reading(matter_id, actor_id) as repo:
                selected = next((accounts for candidate, accounts in self._candidate_pairs(repo, matter_id)
                                 if candidate.candidate_id == candidate_id), None)
            with self.repository.transaction(matter_id, actor_id) as repo:
                previous = repo.connection.execute(
                    "SELECT state FROM workbench_witness_inconsistency_review WHERE matter_id=? AND candidate_id=?",
                    (matter_id, candidate_id)).fetchone()
                if selected is not None:
                    account_ids = tuple(row["account_id"] for row in selected)
                    if self._accounts(repo, matter_id, account_ids) != selected:
                        raise WorkspaceProblem("The current inconsistency candidate changed. Refresh before reviewing.")
                elif previous is None:
                    raise WorkspaceProblem("Choose a current inconsistency candidate and review state.")
                old_state = previous[0] if previous else "unreviewed"
                if old_state == state:
                    return
                if state == "unreviewed":
                    repo.connection.execute(
                        "DELETE FROM workbench_witness_inconsistency_review WHERE matter_id=? AND candidate_id=?",
                        (matter_id, candidate_id))
                else:
                    repo.connection.execute(
                        "INSERT INTO workbench_witness_inconsistency_review VALUES (?,?,?,?,?) "
                        "ON CONFLICT(matter_id,candidate_id) DO UPDATE SET state=excluded.state,"
                        "actor_id=excluded.actor_id,updated_at=excluded.updated_at",
                        (matter_id, candidate_id, state, actor_id, repo.now()))
                repo.entities.audit(actor_principal_id=actor_id, session_id=None, matter_id=matter_id,
                    request_id=candidate_id, action="witness_inconsistency.review", outcome="success",
                    object_type="witness_inconsistency", object_id=candidate_id,
                    details=dict(old_state=old_state, new_state=state))

    def reopen(self, matter_id, actor_id, candidate_id):
        self._set_state(matter_id, actor_id, candidate_id, "unreviewed")

    def mark_reviewed(self, matter_id, actor_id, candidate_id):
        self._set_state(matter_id, actor_id, candidate_id, "reviewed")

    def dismiss(self, matter_id, actor_id, candidate_id):
        self._set_state(matter_id, actor_id, candidate_id, "dismissed")
