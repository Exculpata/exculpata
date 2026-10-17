"""Historical research presentation must preserve its stored source basis."""
from __future__ import annotations

from tests.fixture_write import fixture_write

import copy
import io
import json
import zipfile
from xml.etree import ElementTree

import pytest

from case_intelligence.answer_presentation import (
    GENERATED_ANSWER_INTRODUCTION, GENERATED_REVIEW_NOTICE,
    GENERATED_TRANSCRIPT_INTRODUCTION, answer_content,
)
from tests.test_report_review_basis import ACTOR, convert, saved_research, workspace


def _readable(body, format_name):
    if format_name == "docx":
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            return " ".join(ElementTree.fromstring(archive.read("word/document.xml")).itertext())
    return body.decode()


@pytest.mark.parametrize("introduction,expected", [
    ("The searchable sources support this answer:", GENERATED_ANSWER_INTRODUCTION),
    ("The searchable sources support these findings:", GENERATED_ANSWER_INTRODUCTION),
    ("The machine transcript supports this orientation:", GENERATED_TRANSCRIPT_INTRODUCTION),
    ("The machine transcript supports these orientation points:", GENERATED_TRANSCRIPT_INTRODUCTION),
    ("Synthetic custom introduction:", "Synthetic custom introduction:"),
])
def test_historical_research_view_exports_and_report_keep_original_basis(workspace, introduction, expected):
    client, bench, matter = workspace
    job, document = saved_research(bench, matter)
    result = copy.deepcopy(job.result)
    # Recreate a saved pre-mitigation VerifiedAnswer.text and its structured
    # answer, including the independent per-search finding path.
    result["answer"]["introduction"] = introduction
    result["summary"] = introduction + "\n" + result["summary"]
    result["passes"][0]["answer"]["introduction"] = introduction
    result["passes"][0]["text"] = introduction + "\n" + result["passes"][0]["text"]
    encoded = json.dumps(result)
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute(
            "UPDATE workbench_research_job SET result_json=? WHERE job_id=?", (encoded, job.job_id),
        )
    snapshot = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    original_units = document.parsed_units()
    url = f"/matters/{matter.slug}/research/{job.job_id}"
    page = client.get(f"/matters/{matter.slug}/research?job={job.job_id}")
    assert page.status_code == 200
    assert expected in page.text and GENERATED_REVIEW_NOTICE in page.text
    if expected != introduction:
        assert introduction not in page.text
    for format_name in ("json", "markdown", "docx"):
        download = client.get(url + "/export", params={"format": format_name})
        assert download.status_code == 200, download.text
        text = _readable(download.content, format_name)
        assert expected in text and GENERATED_REVIEW_NOTICE in text
        if expected != introduction:
            assert introduction not in text
        if format_name == "json":
            investigation = download.json()["investigation"]
            assert investigation["answer"]["introduction"] == expected
            assert investigation["synthesis"].startswith(expected + "\n")
            assert investigation["findings"][0]["finding"].startswith(expected + "\n")
            assert investigation["supporting_sources"][0]["excerpt"] == result["evidence"][0]["excerpt"]
    bundle = client.get(f"/matters/{matter.slug}/export")
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = [name for name in archive.namelist() if name.startswith("investigations/")]
        assert names
        for name in names:
            text = _readable(archive.read(name), "docx" if name.endswith(".docx") else "text")
            assert expected in text
            if expected != introduction:
                assert introduction not in text
    report = convert(client, bench, matter, snapshot)
    sections = bench.workspace.report_sections(matter.matter_id, report.report_id)
    for index in (1, 2):
        assert expected in sections[index].body
        assert GENERATED_REVIEW_NOTICE in sections[index].body
        if expected != introduction:
            assert introduction not in sections[index].body
    for format_name in ("markdown", "docx"):
        download = client.get(f"/matters/{matter.slug}/reports/{report.report_id}/export?format={format_name}")
        assert download.status_code == 200
        text = _readable(download.content, format_name)
        assert expected in text and GENERATED_REVIEW_NOTICE in text
        if expected != introduction:
            assert introduction not in text
    assert bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id) == snapshot
    assert bench.workspace.connection.execute(
        "SELECT result_json FROM workbench_research_job WHERE job_id=?", (job.job_id,),
    ).fetchone()[0] == encoded
    assert document.parsed_units() == original_units


def test_presentation_changes_only_a_matching_leading_introduction():
    old = "The searchable sources support this answer:"
    quoted = 'The source quotes "' + old + '" verbatim.'
    assert answer_content(old + "\n" + quoted, old) == GENERATED_ANSWER_INTRODUCTION + "\n" + quoted
    assert answer_content(quoted, old) == quoted
    assert answer_content(old + " followed by unrelated prose", old) == old + " followed by unrelated prose"


@pytest.mark.parametrize("mismatch", ["summary_introduction", "summary_claim", "finding"])
def test_mismatched_historical_summary_is_rejected_before_presentation(workspace, mismatch):
    client, bench, matter = workspace
    job, document = saved_research(bench, matter)
    result = copy.deepcopy(job.result)
    result["answer"]["introduction"] = "The searchable sources support this answer:"
    result["summary"] = result["answer"]["introduction"] + "\n" + result["summary"]
    if mismatch == "summary_introduction":
        # These forms would display identically after normalization; that must
        # never repair the raw mismatch, including on the research GET page.
        result["summary"] = result["summary"].replace(
            result["answer"]["introduction"], GENERATED_ANSWER_INTRODUCTION, 1,
        )
    elif mismatch == "summary_claim":
        result["summary"] = result["answer"]["introduction"] + "\nSynthetic incompatible claim."
    else:
        result["passes"][0]["text"] = "Synthetic incompatible finding."
    encoded = json.dumps(result)
    original_units = document.parsed_units()
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute(
            "UPDATE workbench_research_job SET result_json=? WHERE job_id=?",
            (encoded, job.job_id),
        )
    page = client.get(f"/matters/{matter.slug}/research?job={job.job_id}")
    assert page.status_code == 200
    assert "checkpoint could not be verified" in page.text
    assert "Its derived text is hidden" in page.text
    assert 'class="research-summary prose"' not in page.text
    assert "Synthetic incompatible" not in page.text
    for format_name in ("json", "markdown", "docx"):
        response = client.get(
            f"/matters/{matter.slug}/research/{job.job_id}/export?format={format_name}",
        )
        assert response.status_code == 409
    assert client.get(f"/matters/{matter.slug}/export").status_code == 409
    response = client.post(f"/matters/{matter.slug}/research/{job.job_id}/report", follow_redirects=False)
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert not bench.workspace.reports(matter.matter_id, ACTOR)
    assert bench.workspace.connection.execute(
        "SELECT result_json FROM workbench_research_job WHERE job_id=?", (job.job_id,),
    ).fetchone()[0] == encoded
    assert document.parsed_units() == original_units
