"""Synthetic proving and fail-closed tests for Slice 5 rich discovery tools."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from case_intelligence.case_assistant_tools import (
    AssistantAccessError, AssistantToolError, MatterToolRuntime, query_table_rows,
)
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workbench_assistant import WorkbenchToolBackend
from tests.test_milestone_a_workbench import _create_matter
from tests.test_review_tools import CleanScanner


def _eml(msg_id: str, subject: str, body: str, *, in_reply_to: str = "", date: str = "Thu, 01 Sep 2024 10:00:00 +0000") -> bytes:
    headers = [
        "From: sender@example.invalid", "To: recipient@example.invalid",
        f"Date: {date}", f"Message-ID: <{msg_id}@example.invalid>", f"Subject: {subject}",
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: <{in_reply_to}@example.invalid>")
        headers.append(f"References: <{in_reply_to}@example.invalid>")
    return ("\r\n".join([*headers, "MIME-Version: 1.0", "Content-Type: text/plain; charset=us-ascii", "", body, ""])).encode("ascii")


def test_query_table_exact_filtering_and_decimal_aggregation():
    csv_text = (
        "wire_id,from_account,amount,date\n"
        "w-1,stk-0,12500.50,2024-09-01\n"
        "w-2,stk-1,4000.00,2024-09-02\n"
        "w-3,stk-0,7500.25,2024-09-05\n"
    )
    payload, receipt = query_table_rows(
        "doc-csv", "wires.csv", csv_text,
        filters={"from_account": "stk-0", "amount": {"min": "5000"}},
        aggregate_column="amount", aggregate_op="sum", receipt_id="calc-1",
    )
    assert payload["matched_rows"] == 2
    assert receipt is not None and receipt.result == "20000.75"
    with pytest.raises(AssistantToolError):
        query_table_rows("doc-csv", "wires.csv", csv_text, aggregate_column="amount", aggregate_op="median")


def test_workbench_rich_tools_csv_email_thread_and_transcript_window(tmp_path):
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test", malware_scanner=CleanScanner(), malware_scan_mode="extended")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Rich Tools Matter")
        wires_csv = b"wire_id,from_account,amount\nw-1,stk-0,10000.50\nw-2,stk-1,2500.00\nw-3,stk-0,5000.25\n"
        res = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("wires.csv", wires_csv, "text/csv")),
            ("files", ("mail-1.eml", _eml("m1", "Order schedule", "Initial schedule attached."), "message/rfc822")),
            ("files", ("mail-2.eml", _eml("m2", "Re: Order schedule", "Confirmed receipt of schedule.", in_reply_to="m1", date="Thu, 01 Sep 2024 11:00:00 +0000"), "message/rfc822")),
            ("files", ("mail-3.eml", _eml("m3", "Re: Order schedule", "Placing trade tomorrow.", in_reply_to="m2", date="Thu, 01 Sep 2024 12:00:00 +0000"), "message/rfc822")),
            ("files", ("call-transcript.txt", b"[00:00:10] Speaker 1: Opening call.\n[00:01:30] Speaker 2: Discussed the 2024-09-01 buyback.\n[00:04:15] Speaker 1: Closing remarks.\n", "text/plain")),
        ])
        assert res.status_code == 200
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        runtime = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))

        sources = {s["source_name"]: s["document_id"] for s in runtime.invoke("list_sources", {"limit": 20})["sources"]}
        # 1. query_table over real uploaded wires.csv
        table = runtime.invoke("query_table", {
            "document_id": sources["wires.csv"], "filters": {"from_account": "stk-0"},
            "aggregate_column": "amount", "aggregate_op": "sum",
        })
        assert table["matched_rows"] == 2 and table["calculation"]["result"] == "15000.75"
        assert "calc-1" in runtime.receipts

        # 2. read_email_thread starting from middle message returns all 3 linked emails
        thread = runtime.invoke("read_email_thread", {"document_id": sources["mail-2.eml"]})
        assert thread["count"] == 3
        names = [m["source_name"] for m in thread["messages"]]
        assert set(names) == {"mail-1.eml", "mail-2.eml", "mail-3.eml"}

        # 3. read_transcript_window returns only timestamped lines inside [60s, 180s]
        window = runtime.invoke("read_transcript_window", {
            "document_id": sources["call-transcript.txt"], "start_seconds": 60, "end_seconds": 180,
        })
        assert window["count"] == 1 and "buyback" in window["cues"][0]["text"]

        # 4. Fail-closed checks: scoped runtime refuses out-of-scope doc; negative time refused
        scoped = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor), allowed_document_ids=frozenset({sources["wires.csv"]}))
        with pytest.raises(AssistantAccessError):
            scoped.invoke("read_email_thread", {"document_id": sources["mail-2.eml"]})
        with pytest.raises(AssistantToolError):
            runtime.invoke("read_transcript_window", {"document_id": sources["call-transcript.txt"], "start_seconds": -5})
