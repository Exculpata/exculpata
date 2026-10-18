"""Narrow synthetic tests for the streamed full-text input contract."""
from tests.fixture_write import fixture_write
from copy import deepcopy
import hashlib
import io
import json
import sqlite3
from threading import RLock
import zipfile

import pytest

from case_intelligence.full_text_review import compact_locator
from case_intelligence.full_text_synthesis import FullTextSynthesisService, input_notice, validate_prepared
from case_intelligence.full_text_synthesis_repository import ADMISSION, FullTextSynthesisRepository
from case_intelligence.generation import GroundedGenerationService
from case_intelligence.hierarchical_synthesis import synthesis_answer
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_full_text_synthesis import ACTOR, finish, queue, seed_terminal_review, workspace


class SyntheticLedger:
    def __init__(self):
        self._lock = RLock()
        self.connection = sqlite3.connect(":memory:")
        with self._lock:
            self.connection.row_factory = sqlite3.Row
        self.allowed = True
        self.originals = {}
        self.resolutions = []
        with fixture_write(self) as connection:
            connection.executescript("""
                CREATE TABLE workbench_review_run(run_id TEXT,matter_id TEXT,run_kind TEXT,state TEXT,
                    criterion_id TEXT,criterion_version_id TEXT,source_set_id TEXT,updated_at TEXT);
                CREATE TABLE workbench_text_review(run_id TEXT,policy_json TEXT);
                CREATE TABLE workbench_text_review_budget(run_id TEXT,legacy INTEGER,limit_reason TEXT);
                CREATE TABLE workbench_review_criterion_version(matter_id TEXT,criterion_version_id TEXT,
                    instructions TEXT,include_guidance TEXT,exclude_guidance TEXT);
                CREATE TABLE workbench_review_criterion(matter_id TEXT,criterion_id TEXT,title TEXT);
                CREATE TABLE workbench_text_review_source(run_id TEXT,document_id TEXT,source_version_id TEXT,
                    source_basis_digest TEXT,source_state TEXT,state TEXT,inventory_sealed INTEGER,
                    unit_count INTEGER,text_chars INTEGER,chunk_count INTEGER,empty_units INTEGER);
                CREATE TABLE workbench_source_catalog(matter_id TEXT,document_id TEXT,version_id TEXT,
                    content_basis_digest TEXT,source_state TEXT);
                CREATE TABLE workbench_text_review_unit(run_id TEXT,document_id TEXT,unit_ordinal INTEGER,
                    text_chars INTEGER,unit_digest TEXT,citation_json TEXT,state TEXT);
                CREATE TABLE workbench_text_review_chunk(run_id TEXT,document_id TEXT,unit_ordinal INTEGER,
                    cursor INTEGER,state TEXT,decision TEXT,rationale TEXT,finding_key TEXT);
                CREATE TABLE workbench_review_decision(run_id TEXT,ordinal INTEGER,human_decision TEXT,
                    human_note TEXT,updated_at TEXT);
                CREATE TABLE workbench_text_review_counter(run_id TEXT,kind TEXT,state TEXT,count INTEGER,characters INTEGER);
                CREATE TABLE workbench_source_set(matter_id TEXT,source_set_id TEXT);
                CREATE TABLE workbench_source_set_item(matter_id TEXT,source_set_id TEXT,document_id TEXT);
                INSERT INTO workbench_review_run VALUES ('run','matter','full','succeeded','criterion','criterion-v1',NULL,'run-time');
                INSERT INTO workbench_text_review VALUES ('run','{}');
                INSERT INTO workbench_text_review_budget VALUES ('run',0,'');
                INSERT INTO workbench_review_criterion_version VALUES ('matter','criterion-v1','What do the dispatch records say about delivery?','','');
                INSERT INTO workbench_review_criterion VALUES ('matter','criterion','Dispatch review');
                INSERT INTO workbench_review_decision VALUES ('run',1,'','', 'decision-time');
            """)
        self.document_id = "a" * 32
        with fixture_write(self):
            self.connection.execute("INSERT INTO workbench_source_catalog VALUES (?,?,?,?,?)", ("matter", self.document_id, "b" * 32, "c" * 64, "ready"))
            self.connection.execute("INSERT INTO workbench_text_review_source VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            ("run", self.document_id, "b" * 32, "c" * 64, "ready", "inventoried", 1, 0, 0, 0, 0))
        self.repository = FullTextSynthesisRepository(connection=self.connection, lock=self._lock, authorize=self.authorize)
        self.service = FullTextSynthesisService(repository=self.repository, resolve_originals=self.resolve)

    def authorize(self, matter, actor):
        if matter != "matter" or actor != "reviewer" or not self.allowed:
            raise KeyError("Synthetic authorization revoked")

    def add(self, number, text=None, *, rationale=None, state="processed", decision="include", unit_state=None):
        text = text if text is not None else f"Synthetic dispatch record {number} states the crate was delivered."
        payload = {"matter_id": "matter", "document_id": self.document_id, "source_version_id": "b" * 32,
                   "source_name": "Synthetic dispatch.txt", "location": f"unit {number}", "excerpt": text,
                   "excerpt_digest": hashlib.sha256(text.encode()).hexdigest(), "chunk_id": f"unit-{number}",
                   "support_token": hashlib.sha1(f"synthetic-{number}".encode()).hexdigest(), "unit_number": number,
                   "line_start": None, "line_end": None, "evidence_kind": "document", "href": "/matters/synthetic"}
        self.originals[number] = payload
        locator = compact_locator(payload, number, text)
        with fixture_write(self):
            self.connection.execute("INSERT INTO workbench_text_review_unit VALUES (?,?,?,?,?,?,?)",
                            ("run", self.document_id, number, len(text), payload["excerpt_digest"], json.dumps(locator), unit_state or state))
            if text:
                self.connection.execute("INSERT INTO workbench_text_review_chunk VALUES (?,?,?,?,?,?,?,?)",
                                ("run", self.document_id, number, number, state, decision, text if rationale is None else rationale, f"finding-{number}"))
            self.connection.execute("UPDATE workbench_text_review_source SET unit_count=unit_count+1,text_chars=text_chars+?,chunk_count=chunk_count+?,empty_units=empty_units+?",
                            (len(text), bool(text), not text))

    def resolve(self, locators, *, source_basis_digests, source_versions):
        assert source_basis_digests == {self.document_id: "c" * 64}
        assert source_versions == {self.document_id: "b" * 32}
        self.resolutions.append(deepcopy(locators))
        return [self.originals[locator["unit_ordinal"]] for locator in locators]

    def prepare(self):
        return self.service.prepare("matter", "reviewer", "run")

    def check(self, receipt, **kwargs):
        with fixture_write(self) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.repository.validate_locked("matter", "reviewer", receipt, **kwargs)


@pytest.fixture
def ledger():
    value = SyntheticLedger()
    yield value
    value.connection.close()  # fixture-write: Close this test-owned in-memory ledger after its fixture finishes; it has no workspace workers.


class Echo:
    available = True
    def generate(self, **kwargs):
        return {"answerable": True, "claims": [{"text": source.excerpt, "evidence_ids": [source.evidence_id]}
                                               for source in kwargs["evidence"]],
                "limitation": None, "missing_information": ""}


def test_shared_hierarchy_preserves_late_competing_original_and_charged_restart(ledger):
    for index in range(1, 25):
        ledger.add(index, None if index != 24 else "Synthetic dispatch record 24 states the crate was not delivered.")
    prepared = ledger.prepare()
    ledger.service.generator = GroundedGenerationService(Echo())
    receipt = prepared["full_text_synthesis_input"]
    saved = []
    def checkpoint(state):
        saved.append(deepcopy(state))
        if state["requests_spent"] == 3 and len(state["issue"]) == 2:
            raise RuntimeError("Synthetic interruption")
    with pytest.raises(RuntimeError, match="interruption"):
        ledger.service.run(receipt["question"], prepared, checkpoint, lambda: ledger.check(receipt), now=lambda: 1000)
    result = ledger.service.run(receipt["question"], prepared, lambda state: None, lambda: ledger.check(receipt), saved[-1], now=lambda: 1000)
    answer = synthesis_answer(result, prepared["evidence"])
    assert result["requests_spent"] == 13
    assert result["issue"][:2] == saved[-1]["issue"]
    assert len(answer.claims) == 24
    assert "was not delivered" in answer.text
    assert len(prepared["evidence"]) == 24
    assert not receipt["partial"]


def test_mixed_outcomes_include_long_original_late_text_and_unsupported_finding(ledger):
    ledger.add(1)
    ledger.add(2, "Ordinary synthetic dispatch entry.", rationale="A submarine carried 9999 satellites to Jupiter.")
    ledger.add(3, "Synthetic neutral range.", decision="exclude")
    ledger.add(4, "Synthetic failed range.", state="failed", decision="")
    ledger.add(5, "", unit_state="empty")
    ledger.add(6, "Synthetic context. " * 400 + "The decisive late dispatch account contradicts delivery.")
    prepared = ledger.prepare()
    receipt = validate_prepared(prepared)
    assert receipt["outcomes"] == [[1, "admitted"], [2, "unsupported_finding"], [6, "oversized_original"]]
    assert receipt["oversized_originals"] == [[6, ledger.document_id, 6, len(ledger.originals[6]["excerpt"])]]
    assert receipt["coverage"]["ranges"] == {"no_finding": 1, "failed": 1}
    assert receipt["coverage"]["units"]["empty"] == 1
    assert receipt["partial"]
    assert "oversized original" in input_notice(receipt)
    assert "1 failed analysis ranges" in input_notice(receipt)
    assert [item["unit_ordinal"] for item in ledger.resolutions[0]] == [1, 2]
    assert "Jupiter" not in json.dumps(prepared)
    assert "decisive late" not in json.dumps(prepared)


def test_capture_retains_only_bounded_rationales_but_accounts_for_every_candidate(ledger):
    for number in range(1, 101):
        ledger.add(number)
    captured = ledger.repository.capture("matter", "reviewer", "run")
    assert len(captured["candidates"]) == ADMISSION["findings"] == 48
    assert len(captured["receipt"]["outcomes"]) == 100
    assert captured["receipt"]["outcomes"][-1] == [100, "finding_limit"]
    prepared = ledger.prepare()
    assert prepared["full_text_synthesis_input"]["counts"] == {"candidate_findings": 100, "admitted": 48, "finding_limit": 52}
    assert prepared["full_text_synthesis_input"]["partial"]


def test_human_edit_without_run_timestamp_change_invalidates_active_but_keeps_history(ledger):
    ledger.add(1)
    receipt = ledger.prepare()["full_text_synthesis_input"]
    ledger.check(receipt)
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_decision SET human_decision='exclude',human_note='Synthetic reviewer correction',updated_at='new-decision-time'")
    assert ledger.connection.execute("SELECT updated_at FROM workbench_review_run").fetchone()[0] == receipt["run_updated_at"]
    with pytest.raises(WorkspaceProblem, match="human decision changed"):
        ledger.check(receipt)
    ledger.check(receipt, historical=True)
    assert receipt["human_decisions"]["counts"] == {"unreviewed": 1}


@pytest.mark.parametrize("change", ["DELETE FROM workbench_source_catalog", "UPDATE workbench_source_catalog SET version_id='new'", "UPDATE workbench_source_catalog SET content_basis_digest='new'"])
def test_source_revocation_invalidates_active_and_historical_receipts(ledger, change):
    ledger.add(1)
    receipt = ledger.prepare()["full_text_synthesis_input"]
    with fixture_write(ledger) as connection:
        connection.execute(change)
    for historical in (False, True):
        with pytest.raises(WorkspaceProblem, match="source is missing"):
            ledger.check(receipt, historical=historical)


@pytest.mark.parametrize("state", ["running", "queued"])
def test_active_run_refusal(ledger, state):
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_run SET state=?", (state,))
    with pytest.raises(WorkspaceProblem, match="terminal"):
        ledger.prepare()


def test_wrong_matter_membership_and_new_question_refuse(ledger):
    ledger.add(1)
    prepared = ledger.prepare()
    with pytest.raises(WorkspaceProblem, match="exact criterion"):
        ledger.service.run("Another question", prepared, lambda state: None, lambda: None)
    with pytest.raises(KeyError):
        ledger.repository.capture("different-matter", "reviewer", "run")
    ledger.allowed = False
    with pytest.raises(KeyError):
        ledger.check(prepared["full_text_synthesis_input"])


def test_overlong_criterion_refused_without_truncation(ledger):
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_criterion_version SET instructions=?", ("x" * 2001,))
    with pytest.raises(WorkspaceProblem, match="cannot be truncated"):
        ledger.prepare()


def test_source_set_addition_before_and_after_admission_refuses(ledger):
    ledger.add(1)
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_run SET source_set_id='set'")
        ledger.connection.execute("INSERT INTO workbench_source_set VALUES ('matter','set')")
        ledger.connection.execute("INSERT INTO workbench_source_set_item VALUES ('matter','set',?)", (ledger.document_id,))
    receipt = ledger.prepare()["full_text_synthesis_input"]
    with fixture_write(ledger):
        ledger.connection.execute("INSERT INTO workbench_source_set_item VALUES ('matter','set','new-source')")
    with pytest.raises(WorkspaceProblem, match="source set changed"):
        ledger.check(receipt)
    with pytest.raises(WorkspaceProblem, match="source set changed"):
        ledger.prepare()


@pytest.mark.parametrize("field", ["source", "claim", "omission", "digest"])
def test_saved_input_corruption_is_rejected(ledger, field):
    ledger.add(1)
    prepared = ledger.prepare()
    if field == "source":
        prepared["evidence"][0]["excerpt"] = "Unrelated synthetic assertion."
    elif field == "claim":
        prepared["passes"][0]["answer"]["claims"][0]["text"] = "Unrelated synthetic assertion."
    elif field == "omission":
        prepared["full_text_synthesis_input"]["outcomes"] = []
    else:
        prepared["full_text_synthesis_input"]["input_digest"] = "0" * 64
    with pytest.raises(ValueError):
        validate_prepared(prepared)


def test_source_scope_read_uses_callers_reader_and_preserves_transaction_ownership(ledger):
    ledger.add(1)
    receipt = ledger.prepare()["full_text_synthesis_input"]
    ledger.repository.validate_source_scope_locked(receipt)
    assert not ledger.connection.in_transaction
    # A different reader's authorization belongs to the route, not the original
    # creator recorded in a now-historical input receipt.
    ledger.allowed = False
    with fixture_write(ledger) as connection:
        connection.execute("BEGIN")
        ledger.repository.validate_source_scope_locked(receipt)
        assert ledger.connection.in_transaction


def test_persistence_byte_admission_omits_whole_findings_without_slicing(ledger, monkeypatch):
    for number in range(1, 25):
        ledger.add(number)
    monkeypatch.setitem(ADMISSION, "input_bytes", 12_000)
    prepared = ledger.prepare()
    receipt = validate_prepared(prepared)
    assert receipt["counts"]["input_byte_limit"] > 0
    assert receipt["partial"]
    assert all(source["excerpt"] == ledger.originals[source["unit_number"]]["excerpt"] for source in prepared["evidence"])


def test_complete_receipt_byte_limit_refuses_admission(ledger, monkeypatch):
    ledger.add(1)
    monkeypatch.setitem(ADMISSION, "receipt_bytes", 100)
    with pytest.raises(WorkspaceProblem, match="receipt exceeds"):
        ledger.prepare()


def test_mid_generation_human_edit_refuses_intermediate_save(ledger):
    ledger.add(1)
    prepared = ledger.prepare()
    receipt = prepared["full_text_synthesis_input"]
    class EditingEcho(Echo):
        def generate(self, **kwargs):
            with fixture_write(ledger):
                ledger.connection.execute("UPDATE workbench_review_decision SET human_decision='exclude',updated_at='edited-during-model'")
            return super().generate(**kwargs)
    ledger.service.generator = GroundedGenerationService(EditingEcho())
    saved = []
    with pytest.raises(WorkspaceProblem, match="human decision changed"):
        ledger.service.run(receipt["question"], prepared, lambda state: saved.append(deepcopy(state)), lambda: ledger.check(receipt), now=lambda: 1000)
    assert saved[-1]["requests_spent"] == 1
    assert not saved[-1]["issue"]


def test_complete_criterion_guidance_reaches_generator_and_frozen_export_input(ledger):
    instructions = "Summarize the saved delivery accounts.\nKeep original record numbers."
    include = "Include decisive amber-bicycle dispatch support."
    exclude = "Exclude unsourced violet-submarine speculation."
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_criterion_version SET instructions=?,include_guidance=?,exclude_guidance=?",
                          (instructions, include, exclude))
    ledger.add(1)
    prepared = ledger.prepare()
    receipt = validate_prepared(prepared)
    expected = f"Instructions:\n{instructions}\n\nInclude guidance:\n{include}\n\nExclude guidance:\n{exclude}"
    assert receipt["question"] == expected
    assert len(expected) <= 2_000
    received = []
    class QuestionEcho(Echo):
        def generate(self, **kwargs):
            received.append(kwargs["question"])
            return super().generate(**kwargs)
    ledger.service.generator = GroundedGenerationService(QuestionEcho())
    ledger.service.run(receipt["question"], prepared, lambda state: None, lambda: ledger.check(receipt), now=lambda: 1000)
    # The existing generation service normalizes question whitespace on the
    # model wire; every labelled criterion field must still arrive intact.
    assert received and all(value == " ".join(expected.split()) for value in received)
    # The canonical question is persisted unchanged in the portable input
    # receipt; complete result exports bind job.question to this same value.
    assert json.loads(json.dumps(prepared))["full_text_synthesis_input"]["question"] == expected
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_criterion_version SET exclude_guidance=?", (exclude + " Revised.",))
    changed = ledger.prepare()["full_text_synthesis_input"]
    assert changed["criterion_digest"] != receipt["criterion_digest"]
    assert changed["snapshot_digest"] != receipt["snapshot_digest"]
    with pytest.raises(WorkspaceProblem, match="changed"):
        ledger.check(receipt)


@pytest.mark.parametrize("instructions,include,exclude", [
    ("x" * 8_000, "i" * 4_000, "e" * 4_000),
    ("Summarize the delivery accounts.", "i" * 2_000, ""),
    ("Summarize the delivery accounts.", "", "e" * 2_000),
    ("s" * 1_000, "i" * 500, "e" * 500),
])
def test_complete_criterion_overflow_refuses_without_dropping_guidance(ledger, instructions, include, exclude):
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_criterion_version SET instructions=?,include_guidance=?,exclude_guidance=?",
                          (instructions, include, exclude))
    with pytest.raises(WorkspaceProblem, match="complete review criterion exceeds"):
        ledger.prepare()
    assert ledger.resolutions == []


def test_empty_guidance_has_no_placeholder_and_exact_instruction_text_is_retained(ledger):
    instructions = "Read these saved accounts.\n\nKeep competing statements separate."
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_criterion_version SET instructions=?,include_guidance='',exclude_guidance=''", (instructions,))
    ledger.add(1)
    assert ledger.prepare()["full_text_synthesis_input"]["question"] == instructions


def test_instruction_only_question_accepts_exactly_2000_characters_without_labels(ledger):
    instructions = "x" * 2_000
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_criterion_version SET instructions=?,include_guidance='',exclude_guidance=''", (instructions,))
    ledger.add(1)
    assert ledger.prepare()["full_text_synthesis_input"]["question"] == instructions
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_criterion_version SET instructions=?", (instructions + "x",))
    with pytest.raises(WorkspaceProblem, match="complete review criterion exceeds"):
        ledger.prepare()


@pytest.mark.parametrize("backend", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("guidance", [False, True])
def test_maximum_criterion_reaches_both_generation_requests_once_with_exclusion_tail(ledger, monkeypatch, backend, guidance):
    from case_intelligence import generation
    from case_intelligence.full_text_synthesis_repository import criterion_question

    beginning = "Summarize the saved delivery accounts. "
    exclusion = "Exclude violet-submarine speculation."
    criterion = {"instructions": beginning,
                 "include_guidance": "Include original dispatch statements." if guidance else "",
                 "exclude_guidance": exclusion if guidance else ""}
    if not guidance:
        criterion["instructions"] += exclusion
    padding = "x" * (2_000 - len(criterion_question(criterion)))
    criterion["instructions"] = (beginning + padding if guidance else beginning + padding + exclusion)
    expected = criterion_question(criterion)
    assert len(expected) == 2_000 and expected.endswith(exclusion)
    with fixture_write(ledger):
        ledger.connection.execute("UPDATE workbench_review_criterion_version SET instructions=?,include_guidance=?,exclude_guidance=?",
            tuple(criterion[key] for key in ("instructions", "include_guidance", "exclude_guidance")))
    ledger.add(1)
    prepared = ledger.prepare()
    assert prepared["full_text_synthesis_input"]["question"] == expected

    # Exercise the real service, client prompt builder and transport payload;
    # only transport and readiness I/O are replaced with synthetic responses.
    requests = []
    def request(url, payload, **kwargs):
        requests.append(deepcopy(payload))
        answer = {"answerable": True, "claims": [{"text": ledger.originals[1]["excerpt"], "evidence_ids": ["S1"]}],
                  "limitation": None, "missing_information": ""}
        message = {"content": json.dumps(answer)}
        return {"message": message} if backend == "ollama" else {"choices": [{"message": message}]}
    monkeypatch.setattr(generation, "_bounded_json_request", request)
    monkeypatch.setattr(generation, "_bounded_json_get", lambda *args, **kwargs:
        {"models": [{"name": "synthetic-generator"}], "data": [{"id": "synthetic-generator"}]})
    client_type = generation.OllamaGenerator if backend == "ollama" else generation.OpenAICompatibleGenerator
    ledger.service.generator = GroundedGenerationService(client_type("http://127.0.0.1:1", "synthetic-generator"))
    receipt = prepared["full_text_synthesis_input"]
    state = ledger.service.run(expected, prepared, lambda state: None, lambda: ledger.check(receipt), now=lambda: 1000)
    assert state["stop_reason"] == "completed" and state["requests_spent"] == len(requests) == 2
    normalized = " ".join(expected.split())
    for level, payload in zip(("issue", "matter"), requests):
        user = payload["messages"][1]["content"]
        context_text, rest = user.split("\n\nQuestion:\n", 1)
        question, _ = rest.split("\n\nMatter evidence:\n", 1)
        assert question == normalized and question.endswith(exclusion)
        assert len(question) <= generation.MAX_QUESTION_CHARS
        context = json.loads(context_text.split(":\n", 1)[1])
        assert f"{level}-level section" in context["task"]
        assert "Retain supporting AND competing accounts" in context["task"]
        assert "Summarize the saved delivery accounts" not in context["task"]
        assert len(context_text) <= generation.MAX_WORKING_CONTEXT_CHARS
        assert (payload["options"]["num_predict"] if backend == "ollama" else payload["max_tokens"]) == 1_200


def test_complete_criterion_is_retained_in_all_final_export_formats(workspace, monkeypatch):
    _, bench, matter, model = workspace
    include = "Include decisive amber-bicycle dispatch support."
    exclude = "Exclude unsourced violet-submarine speculation."
    original = bench.workspace.create_review_criterion
    def complete_criterion(*args, **kwargs):
        return original(*args, **kwargs, include_guidance=include, exclude_guidance=exclude)
    monkeypatch.setattr(bench.workspace, "create_review_criterion", complete_criterion)
    run, _, _ = seed_terminal_review(bench, matter, count=1)
    version = bench.workspace.review_criterion_version(matter.matter_id, run.criterion_version_id)
    expected = f"Instructions:\n{version.instructions}\n\nInclude guidance:\n{include}\n\nExclude guidance:\n{exclude}"
    completed = finish(bench, queue(bench, matter, run))
    assert completed.question == expected
    assert completed.result["full_text_synthesis_input"]["question"] == expected
    assert model.calls and all(call["question"] == " ".join(expected.split()) for call in model.calls)
    for format_name in ("json", "markdown", "docx"):
        artifact = bench.export_research_work_product(matter, completed, format_name)
        if format_name == "json":
            payload = json.loads(artifact.body)["investigation"]
            assert payload["question"] == expected
            assert payload["full_text_synthesis_input"]["question"] == expected
        else:
            if format_name == "docx":
                with zipfile.ZipFile(io.BytesIO(artifact.body)) as archive:
                    body = archive.read("word/document.xml").decode()
            else:
                body = artifact.body.decode()
            assert version.instructions in body and include in body and exclude in body
            assert all(label in body for label in ("Instructions:", "Include guidance:", "Exclude guidance:"))


def test_final_receipt_byte_limit_includes_post_admission_metadata(ledger, monkeypatch):
    from case_intelligence.full_text_synthesis_repository import encoded_bytes
    ledger.add(1)
    captured = ledger.repository.capture("matter", "reviewer", "run")["receipt"]
    monkeypatch.setitem(ADMISSION, "receipt_bytes", encoded_bytes(captured) + 5)
    # The database snapshot fits, but final counts, partial status and digest
    # must fit the same receipt cap before the adapter returns admitted input.
    with pytest.raises(ValueError, match="complete full-text input receipt"):
        ledger.prepare()
