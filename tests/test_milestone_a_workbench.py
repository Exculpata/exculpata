from __future__ import annotations

import io
import re
import time
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from tests.test_evidence_graph_workflow import _LinkParser

ROOT = Path(__file__).parents[1]
PDF = ROOT / "src/case_intelligence/demo_data/synthetic_case_report.pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
WEB_ACTOR = "development-taylor-morgan"


def _docx(*paragraphs: str) -> bytes:
    escaped = [
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for value in paragraphs
    ]
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>'
        + "".join(f"<w:p><w:r><w:t>{value}</w:t></w:r></w:p>" for value in escaped)
        + "<w:sectPr/></w:body></w:document>"
    ).encode()
    types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("word/document.xml", document)
    return output.getvalue()


class EvidenceEchoGenerator:
    available = True

    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        evidence = kwargs["evidence"]
        claims = []
        for item in evidence[:2]:
            claims.append({"text": item.excerpt[:800], "evidence_ids": [item.evidence_id]})
        return {
            "answerable": True,
            "claims": claims,
            "limitation": None,
            "missing_information": "",
        }


class AbstainingGenerator:
    available = True

    def generate(self, **kwargs):
        return {
            "answerable": False,
            "claims": [],
            "limitation": None,
            "missing_information": "No record of a blood alcohol level is present in the supplied sources.",
        }


def _create_matter(client: TestClient, name: str = "Synthetic Matter AURORA-17") -> str:
    response = client.post(
        "/matters",
        data={"name": name, "descriptor": "Training dataset · Review exercise"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    matched = re.fullmatch(r"/matters/(m-[0-9a-f]{12})/setup", response.headers["location"])
    assert matched
    return matched.group(1)


def _upload_pack(client: TestClient, slug: str):
    return client.post(
        f"/matters/{slug}/uploads",
        files=[
            ("files", ("Incident reports.pdf", PDF.read_bytes(), "application/pdf")),
            (
                "files",
                (
                    "Interview notes.docx",
                    _docx(
                        "OFFICER LEE INTERVIEW",
                        "Officer Lee stated the canvas bag was first visible at 10:12 p.m. after the vehicle stopped.",
                    ),
                    DOCX_MIME,
                ),
            ),
            (
                "files",
                (
                    "Evidence inventory.txt",
                    b"The canvas bag was inventoried under training number ROWAN-447.\n",
                    "text/plain",
                ),
            ),
        ],
    )


def _answer_and_wait(
    client: TestClient,
    slug: str,
    conversation_id: str,
    question: str,
    *,
    request_digit: str = "a",
    terminal_states=("succeeded",),
):
    response = client.post(
        f"/matters/{slug}/ask",
        data={
            "conversation": conversation_id,
            "question": question,
            "request_key": f"answer-request-{request_digit * 32}",
        },
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 202
    job = response.json()
    assert job["state"] in {"queued", "running", *terminal_states}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and job["state"] not in terminal_states:
        time.sleep(0.01)
        job = client.get(job["status_url"]).json()
    assert job["state"] in terminal_states
    workspace = client.get(job["workspace_url"].split("#", 1)[0])
    return job, workspace


def test_full_matter_upload_search_generated_answer_support_and_restart(tmp_path):
    runtime = tmp_path / "runtime"
    generator = EvidenceEchoGenerator()
    with TestClient(create_workbench_app(runtime, generator=generator, auth_mode="test")) as client:
        new_matter = client.get("/")
        assert new_matter.url.path == "/matters/new"
        assert "You are using the local preview as" in new_matter.text
        assert "Organization account sign-in is not connected yet" not in new_matter.text
        slug = _create_matter(client)
        setup = client.get(f"/matters/{slug}/setup")
        assert "Drop files here" in setup.text
        assert 'data-max-upload-items="10000"' in setup.text
        assert "up to 10,000 selected records" in setup.text
        assert "resumable 2,000-item batches" in setup.text
        uploaded = _upload_pack(client, slug)
        assert uploaded.status_code == 200
        assert "Incident reports.pdf" in uploaded.text
        assert "Interview notes.docx" in uploaded.text
        assert "Evidence inventory.txt" in uploaded.text
        assert "Searchable" in uploaded.text
        assert str(runtime) not in uploaded.text

        workspace = client.get(f"/matters/{slug}")
        assert 'aria-current="page"' in workspace.text
        assert 'data-rail-toggle' in workspace.text
        assert 'data-rail-switcher' in workspace.text
        assert 'data-rail-collapse' in workspace.text
        assert 'aria-controls="matter-rail"' in workspace.text
        assert 'data-answer-wait-status' in workspace.text
        assert 'role="status"' in workspace.text
        assert 'class="matter-section-tabs"' in workspace.text
        links = [(href, label.strip()) for href, label in _LinkParser(workspace.text).links]
        assert (f"/matters/{slug}/setup?view=list&page_size=100#source-library", "Review") in links
        assert any(href == f"/matters/{slug}/full-review" for href, _ in links)
        assert "Find records matching a rule" in workspace.text
        assert "Case notes" in workspace.text
        assert 'id="conversation-heading">Ask</h2>' in workspace.text
        assert "Request sent" in workspace.text
        assert "Find relevant support" in workspace.text
        assert "Order it for relevance" in workspace.text
        assert "Draft from the best support" in workspace.text
        assert "Check citations and text" in workspace.text

        search = client.get(
            f"/matters/{slug}", params={"mode": "search", "q": "canvas bag inventory"}
        )
        assert search.status_code == 200
        assert 'id="search-results"' in search.text
        assert "Incident reports.pdf" in search.text
        support_token = re.search(
            rf'href="/matters/{slug}\?support=([0-9a-f]{{40}})', search.text
        )
        assert support_token
        support = client.get(
            f"/matters/{slug}?support={support_token.group(1)}"
        )
        assert support.status_code == 200
        assert 'id="support-pane"' in support.text
        assert "Supporting source" in support.text
        assert "<mark>" in support.text

        matter = client.app.state.workbench.matter(slug, WEB_ACTOR)
        conversation = client.app.state.workbench.workspace.get_conversation(matter.matter_id)
        job, answer = _answer_and_wait(
            client,
            slug,
            conversation.conversation_id,
            "What do the sources say about the canvas bag?",
        )
        assert job["stage"] == "complete"
        assert [
            event["stage"]
            for event in job["events"]
            if event["stage"] in {"retrieving", "reranking", "generating", "verifying"}
        ] == ["retrieving", "reranking", "generating", "verifying"]
        assert answer.status_code == 200
        assert 'data-answer-mode="generated"' in answer.text
        assert "Open support" not in answer.text or "Supporting source" not in answer.text
        assert len(generator.calls) == 1
        for item in generator.calls[0]["evidence"]:
            assert item.source_name
        citation = re.search(
            rf'href="(/matters/{slug}\?support=[0-9a-f]{{40}}&amp;conversation=conversation-[0-9a-f]{{32}}#support-pane)"',
            answer.text,
        )
        assert citation

    restarted_generator = EvidenceEchoGenerator()
    with TestClient(
        create_workbench_app(runtime, generator=restarted_generator, auth_mode="test")
    ) as restarted:
        workspace = restarted.get(f"/matters/{slug}")
        assert "Synthetic Matter AURORA-17" in workspace.text
        assert "What do the sources say about the canvas bag?" in workspace.text
        assert 'data-answer-mode="generated"' in workspace.text
        assert "Interview notes.docx" in workspace.text


def test_cross_matter_search_support_and_conversation_isolation(tmp_path):
    generator = EvidenceEchoGenerator()
    with TestClient(
        create_workbench_app(tmp_path / "runtime", generator=generator, auth_mode="test")
    ) as client:
        first = _create_matter(client, "Synthetic Matter AURORA-17")
        _upload_pack(client, first)
        second = _create_matter(client, "State v. Delgado")
        second_upload = client.post(
            f"/matters/{second}/uploads",
            files=[
                (
                    "files",
                    (
                        "Delgado notes.txt",
                        b"The distinct second-matter canary is VIOLET-HARBOR-842.\n",
                        "text/plain",
                    ),
                )
            ],
        )
        assert second_upload.status_code == 200
        first_search = client.get(
            f"/matters/{first}", params={"mode": "search", "q": "ROWAN-447"}
        )
        assert "Evidence inventory.txt" in first_search.text
        token = re.search(rf"/matters/{first}\?support=([0-9a-f]{{40}})", first_search.text).group(1)
        second_search = client.get(
            f"/matters/{second}", params={"mode": "search", "q": "ROWAN-447"}
        )
        assert "No matching record found" in second_search.text
        assert client.get(f"/matters/{second}?support={token}").status_code == 404

        first_matter = client.app.state.workbench.matter(first, WEB_ACTOR)
        first_conversation = client.app.state.workbench.workspace.get_conversation(
            first_matter.matter_id
        )
        second_matter = client.app.state.workbench.matter(second, WEB_ACTOR)
        assert (
            client.get(
                f"/matters/{second}",
                params={"conversation": first_conversation.conversation_id},
            ).status_code
            == 404
        )
        assert "VIOLET-HARBOR-842" not in first_search.text
        assert "Evidence inventory.txt" not in second_search.text


def test_generator_unavailability_is_visible_without_extractive_fallback(tmp_path):
    with TestClient(
        create_workbench_app(
            tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test"
        )
    ) as client:
        slug = _create_matter(client)
        client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "notes.txt",
                        b"The canvas bag was observed at 10:07 p.m.\n",
                        "text/plain",
                    ),
                )
            ],
        )
        matter = client.app.state.workbench.matter(slug, WEB_ACTOR)
        conversation = client.app.state.workbench.workspace.get_conversation(matter.matter_id)
        job, response = _answer_and_wait(
            client,
            slug,
            conversation.conversation_id,
            "When was the canvas bag observed?",
            request_digit="b",
            terminal_states=("failed",),
        )
        assert job["stage"] == "failed"
        assert "temporarily unavailable" in job["message"]
        assert response.status_code == 200
        assert "Answering is temporarily unavailable" in response.text
        assert 'data-answer-mode="generated"' not in response.text
        assert "10:07 p.m." not in response.text
        assert client.get("/health").json()["status"] == "degraded"


def test_model_abstention_is_rendered_as_not_supported_not_generated(tmp_path):
    with TestClient(
        create_workbench_app(
            tmp_path / "runtime", generator=AbstainingGenerator(), auth_mode="test"
        )
    ) as client:
        slug = _create_matter(client)
        client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "notes.txt",
                        b"The canvas bag was observed at 10:07 p.m.\n",
                        "text/plain",
                    ),
                )
            ],
        )
        matter = client.app.state.workbench.matter(slug, WEB_ACTOR)
        conversation = client.app.state.workbench.workspace.get_conversation(
            matter.matter_id
        )
        job, response = _answer_and_wait(
            client,
            slug,
            conversation.conversation_id,
            "What was the blood alcohol level?",
            request_digit="c",
        )
        assert job["state"] == "succeeded"
        assert response.status_code == 200
        assert "could not find enough support" in response.text
        assert 'data-answer-mode="generated"' not in response.text


def test_malformed_source_failure_persists_and_can_be_retried_then_removed(tmp_path):
    runtime = tmp_path / "runtime"
    with TestClient(
        create_workbench_app(
            runtime, generator=EvidenceEchoGenerator(), auth_mode="test"
        )
    ) as client:
        slug = _create_matter(client)
        response = client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "Malformed notes.docx",
                        b"PK\x03\x04not-a-valid-office-archive",
                        DOCX_MIME,
                    ),
                )
            ],
        )
        assert response.status_code == 200
        setup = client.get(f"/matters/{slug}/setup")
        assert "Malformed notes.docx" in setup.text
        assert "damaged or malformed" in setup.text
        retry = re.search(
            rf'action="/matters/{slug}/sources/([0-9a-f]{{32}})/retry"',
            setup.text,
        )
        assert retry

    with TestClient(
        create_workbench_app(
            runtime, generator=EvidenceEchoGenerator(), auth_mode="test"
        )
    ) as restarted:
        setup = restarted.get(f"/matters/{slug}/setup")
        assert "Malformed notes.docx" in setup.text
        token = retry.group(1)
        retried = restarted.post(
            f"/matters/{slug}/sources/{token}/retry",
            follow_redirects=True,
        )
        assert retried.status_code == 200
        assert "damaged or malformed" in restarted.get(f"/matters/{slug}/setup").text
        removed = restarted.post(
            f"/matters/{slug}/sources/{token}/remove",
            follow_redirects=True,
        )
        assert removed.status_code == 200
        assert "Malformed notes.docx" not in restarted.get(f"/matters/{slug}/setup").text


def test_staff_pages_omit_backend_and_storage_details(tmp_path):
    with TestClient(
        create_workbench_app(
            tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test"
        )
    ) as client:
        slug = _create_matter(client)
        page = client.get(f"/matters/{slug}")
        text = page.text.casefold()
        for forbidden in (
            str(tmp_path).casefold(),
            "/home/",
            "postgres",
            "pgvector",
            "granite",
            "modernbert",
            "ollama",
            "model id",
            "vector score",
            "sha256",
        ):
            assert forbidden not in text
        assert not re.search(r"ci-matter-[0-9a-f]{32}", text)
