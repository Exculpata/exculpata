"""Fail-closed comparison of attributed accounts attached to evidence assertions."""

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re

from .workspace_store import WorkspaceProblem

LABEL = "possible inconsistency for review"
_REVIEW_STATES = frozenset(("reviewed", "dismissed"))
_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?:\s*(am|pm))?\b", re.I)
_QUANTITY = re.compile(r"\b\d+(?:\.\d+)?\b")
_PLACE = re.compile(
    r"\b(?:beside|inside|outside|near|at)\s+"
    r"(?!\d|(?:the\s+)?(?:time|noon|least)\b)(?:the\s+)?"
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


def _normalized(value):
    return " ".join(value.casefold().split())


def _time_minutes(text):
    result = set()
    for hours, minutes, marker in _TIME.findall(text):
        hour = int(hours)
        if marker:
            hour = hour % 12 + (12 if marker.casefold() == "pm" else 0)
        result.add(hour * 60 + int(minutes))
    return result


def _differences(first, second):
    """Return only explicit, bounded surface differences; make no truth finding."""
    left_times, right_times = _time_minutes(first), _time_minutes(second)
    if left_times and right_times and left_times != right_times:
        return "time"
    left_places = {_normalized(value) for value in _PLACE.findall(first)}
    right_places = {_normalized(value) for value in _PLACE.findall(second)}
    if left_places and right_places and left_places != right_places:
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

    def _find_current(self, repo, matter_id):
        rows = [dict(row) for row in repo.connection.execute(
            "SELECT a.* FROM workbench_assertion_account a "
            "JOIN workbench_assertion r ON r.matter_id=a.matter_id AND r.assertion_id=a.assertion_id "
            "WHERE a.matter_id=? AND r.record_type='event' ORDER BY a.assertion_id,a.account_id",
            (matter_id,))]
        valid = self.entities.validate_references(rows)
        rows = [row for index, row in enumerate(rows)
                if index in valid and self._citation(row) is not None]
        states = {row["candidate_id"]: row["state"] for row in repo.connection.execute(
            "SELECT candidate_id,state FROM workbench_witness_inconsistency_review WHERE matter_id=?",
            (matter_id,))}
        results = []
        for index, first in enumerate(rows):
            for second in rows[index + 1:]:
                same_witness = _normalized(first["attributed_to"]) == _normalized(second["attributed_to"])
                same_event = first["assertion_id"] == second["assertion_id"]
                if not same_event:
                    continue
                difference = _differences(first["excerpt"], second["excerpt"])
                if not difference:
                    continue
                comparison = "same witness" if same_witness else "different witnesses on the same event"
                candidate = self._candidate(matter_id, first, second, comparison, difference)
                state = states.get(candidate.candidate_id, "unreviewed")
                if state != "dismissed":
                    results.append(replace(candidate, review_state=state))
        return tuple(results)

    def _set_state(self, matter_id, actor_id, candidate_id, state):
        if (state not in _REVIEW_STATES or not isinstance(candidate_id, str)
                or not re.fullmatch(r"witness-inconsistency-[0-9a-f]{64}", candidate_id)):
            raise WorkspaceProblem("Choose a current inconsistency candidate and review state.")
        with self.entities.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            if not any(row.candidate_id == candidate_id for row in self._find_current(repo, matter_id)):
                raise WorkspaceProblem("Choose a current inconsistency candidate and review state.")
            repo.connection.execute(
                "INSERT INTO workbench_witness_inconsistency_review VALUES (?,?,?,?,?) "
                "ON CONFLICT(matter_id,candidate_id) DO UPDATE SET state=excluded.state,"
                "actor_id=excluded.actor_id,updated_at=excluded.updated_at",
                (matter_id, candidate_id, state, actor_id, repo.now()))

    def mark_reviewed(self, matter_id, actor_id, candidate_id):
        self._set_state(matter_id, actor_id, candidate_id, "reviewed")

    def dismiss(self, matter_id, actor_id, candidate_id):
        self._set_state(matter_id, actor_id, candidate_id, "dismissed")
