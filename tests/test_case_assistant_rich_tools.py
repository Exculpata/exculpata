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

        # 4. Fail-closed checks: scoped runtime refuses out-of-scope doc; negative/malformed/NaN time refused
        scoped = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor), allowed_document_ids=frozenset({sources["wires.csv"]}))
        with pytest.raises(AssistantAccessError):
            scoped.invoke("read_email_thread", {"document_id": sources["mail-2.eml"]})
        for bad_args in (
            {"document_id": sources["call-transcript.txt"], "start_seconds": -5},
            {"document_id": sources["call-transcript.txt"], "start_seconds": None},
            {"document_id": sources["call-transcript.txt"], "start_seconds": "bad"},
            {"document_id": sources["call-transcript.txt"], "start_seconds": float("nan")},
            {"document_id": sources["call-transcript.txt"], "start_seconds": 0, "end_seconds": float("inf")},
        ):
            with pytest.raises(AssistantToolError):
                runtime.invoke("read_transcript_window", bad_args)


class _TwoCueTranscriptModel:
    def __init__(self, doc_id: str) -> None:
        self.doc_id = doc_id
        self.step = 0

    def complete_turn(self, messages, *, tools):
        import json
        from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn
        self.step += 1
        if self.step == 1:
            return AssistantTurn(tool_calls=(AssistantModelCall("t1", "read_transcript_window", {
                "document_id": self.doc_id, "start_seconds": 0, "end_seconds": 120,
            }),))
        tool_msg = json.loads(messages[-1]["content"])
        cue = tool_msg["cues"][0]
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "Speaker reported Paid 12345.",
            "findings": [{
                "text": "Speaker reported Paid 12345.",
                "citations": [{
                    "document_id": self.doc_id,
                    "source_name": tool_msg["source_name"],
                    "location": cue["location"],
                    "source_version_id": cue["source_version_id"],
                }],
                "calculation_ids": [],
            }],
            "gaps": [], "competing_accounts": [],
        }))


def test_pr589_regressions_tables_precision_email_spans_integrity_and_cue_accumulation(tmp_path):
    from case_intelligence.case_assistant import CaseAssistant

    # 1. Exact Decimal precision beyond 6 fractional digits is preserved in result and receipt inputs.
    prec_payload, prec_receipt = query_table_rows(
        "doc-p", "micro.csv", "amount\n0.0000001\n0.0000002\n",
        aggregate_column="amount", aggregate_op="sum", receipt_id="calc-p",
    )
    assert prec_receipt is not None and prec_receipt.result == "0.0000003"
    assert [i.value for i in prec_receipt.inputs] == ["0.0000001", "0.0000002"]

    app = create_workbench_app(tmp_path / "runtime", auth_mode="test", malware_scanner=CleanScanner(), malware_scan_mode="extended")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Rich Tool Regressions")
        big_rows = ["id,tag,amount"] + [f"r-{i},other,1" for i in range(1, 2105)] + ["r-2105,target,999"]
        res = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("sparse.csv", b"id,note,amount\nw1,,100\nw2,ok,200\n", "text/csv")),
            ("files", ("single-col.csv", b"amount\n10\n20\n", "text/csv")),
            ("files", ("large.csv", ("\n".join(big_rows) + "\n").encode("utf-8"), "text/csv")),
            ("files", ("body-check.eml", _eml("b1", "Wire notice", "The scheduled amount is 9876."), "message/rfc822")),
            ("files", ("two-cue.txt", b"[00:00:05] S1: Paid 12345.\n[00:00:10] S2: Received 67890.\n", "text/plain")),
        ])
        assert res.status_code == 200
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        backend = WorkbenchToolBackend(bench, actor)
        runtime = MatterToolRuntime(matter.matter_id, actor, backend)
        sources = {s["source_name"]: s["document_id"] for s in runtime.invoke("list_sources", {"limit": 20})["sources"]}

        # 2. Sparse extracted CSV preserves column coordinates (empty cell does not shift amount).
        sparse = runtime.invoke("query_table", {
            "document_id": sources["sparse.csv"], "aggregate_column": "amount", "aggregate_op": "sum",
        })
        assert sparse["calculation"]["result"] == "300"

        # 3. Single-column extracted CSV is detected and aggregated cleanly.
        single = runtime.invoke("query_table", {
            "document_id": sources["single-col.csv"], "aggregate_column": "amount", "aggregate_op": "sum",
        })
        assert single["calculation"]["result"] == "30"

        # 4. Complete table (>2,000 rows, >20 sections) aggregates rows past section 20.
        large = runtime.invoke("query_table", {
            "document_id": sources["large.csv"], "filters": {"tag": "target"},
            "aggregate_column": "amount", "aggregate_op": "sum",
        })
        assert large["matched_rows"] == 1 and large["calculation"]["result"] == "999"

        # 5. read_email_thread cites the email body span (not Email headers).
        thread = runtime.invoke("read_email_thread", {"document_id": sources["body-check.eml"]})
        msg = thread["messages"][0]
        assert "Email headers" not in msg["location"]
        wc = backend.citations_by_key[(msg["document_id"], msg["location"])]
        assert "9876" in wc.excerpt

        # 6. Multi-cue transcript accumulates all cues sharing a location for verification.
        tx_rt = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        tx_res = CaseAssistant(_TwoCueTranscriptModel(sources["two-cue.txt"])).investigate("What was paid?", tx_rt)
        assert tx_res.answerable is True and tx_res.omitted_findings == 0 and len(tx_res.findings) == 1

        # 7. Tampering with uploaded EML bytes on disk fails explicitly instead of reparsing altered bytes.
        store = bench.source_store(matter)
        eml_path = store.source_path(sources["body-check.eml"], verify_digest=True)
        eml_path.write_bytes(_eml("b1", "Wire notice", "Modified amount 9999."))
        with pytest.raises(AssistantToolError):
            runtime.invoke("read_email_thread", {"document_id": sources["body-check.eml"]})


class _RowCountTableModel:
    def __init__(self, doc_id: str) -> None:
        self.doc_id = doc_id
        self.step = 0

    def complete_turn(self, messages, *, tools):
        import json
        from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn
        self.step += 1
        if self.step == 1:
            return AssistantTurn(tool_calls=(AssistantModelCall("tc1", "query_table", {
                "document_id": self.doc_id, "aggregate_op": "count",
            }),))
        tool_msg = json.loads(messages[-1]["content"])
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "There are 3 matching rows.",
            "findings": [{
                "text": "There are 3 matching rows in the table.",
                "citations": [{
                    "document_id": self.doc_id,
                    "source_name": tool_msg["source_name"],
                    "location": tool_msg["location"],
                    "source_version_id": tool_msg["source_version_id"],
                }],
                "calculation_ids": ["calc-1"],
            }],
            "gaps": [], "competing_accounts": [],
        }))


def test_pr589_round2_section_locators_ambiguous_pipe_all_inputs_count_grounding_and_thread_paging(tmp_path):
    from case_intelligence.case_assistant import CaseAssistant

    # 1. Complete receipt inputs (>50 rows): summing fifty 1s + 999 retains all 51 inputs.
    fifty_one = "amount\n" + ("1\n" * 50) + "999\n"
    _, r51 = query_table_rows("doc-51", "fiftyone.csv", fifty_one, aggregate_column="amount", aggregate_op="sum", receipt_id="calc-51")
    assert r51 is not None and r51.result == "1049" and len(r51.inputs) == 51

    app = create_workbench_app(tmp_path / "runtime", auth_mode="test", malware_scanner=CleanScanner(), malware_scan_mode="extended")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Rich Tool Round2")
        sec2_rows = ["id,tag,amount"] + [f"r-{i},pad,1" for i in range(1, 102)] + ["r-102,hit,9876"]
        res = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("sec2.csv", ("\n".join(sec2_rows) + "\n").encode("utf-8"), "text/csv")),
            ("files", ("pipe.csv", b"id,note,amount\nw1,x | y,100\nw2,ok,200\n", "text/csv")),
            ("files", ("three.csv", b"amount\n100\n200\n300\n", "text/csv")),
            ("files", ("t1.eml", _eml("e1", "Thread", "First message.", date="Thu, 01 Sep 2024 10:00:00 +0000"), "message/rfc822")),
            ("files", ("t2.eml", _eml("e2", "Re: Thread", "Second message.", in_reply_to="e1", date="Thu, 01 Sep 2024 11:00:00 +0000"), "message/rfc822")),
            ("files", ("t3.eml", _eml("e3", "Re: Thread", "Third message.", in_reply_to="e2", date="Thu, 01 Sep 2024 12:00:00 +0000"), "message/rfc822")),
            ("files", ("t4.eml", _eml("e4", "Re: Thread", "Fourth message.", in_reply_to="e3", date="Thu, 01 Sep 2024 13:00:00 +0000"), "message/rfc822")),
        ])
        assert res.status_code == 200
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        backend = WorkbenchToolBackend(bench, actor)
        runtime = MatterToolRuntime(matter.matter_id, actor, backend)
        sources = {s["source_name"]: s["document_id"] for s in runtime.invoke("list_sources", {"limit": 20})["sources"]}

        # 2. Match on row 102 (section 2) carries section 2's locator, NOT section 1.
        sec2 = runtime.invoke("query_table", {
            "document_id": sources["sec2.csv"], "filters": {"tag": "hit"},
            "aggregate_column": "amount", "aggregate_op": "sum",
        })
        assert sec2["matched_rows"] == 1
        row_loc = sec2["rows"][0]["_location"]
        assert row_loc != "Page 1" and "2" in row_loc and sec2["calculation"]["inputs"][0]["source_refs"][0]["location"] == row_loc
        wc_sec2 = backend.citations_by_key[(sources["sec2.csv"], row_loc)]
        assert "9876" in wc_sec2.excerpt

        # 3. Literal ' | ' inside uploaded CSV cell is parsed directly from verified original CSV without dropping rows.
        pipe_res = runtime.invoke("query_table", {
            "document_id": sources["pipe.csv"], "aggregate_column": "amount", "aggregate_op": "sum",
        })
        assert pipe_res["matched_rows"] == 2
        assert pipe_res["rows"][0]["note"] == "x | y"
        assert pipe_res["calculation"]["result"] == "300"

        # 4. Row count derivation (3 rows of 100/200/300) passes CaseAssistant verification.
        cnt_rt = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        cnt_res = CaseAssistant(_RowCountTableModel(sources["three.csv"])).investigate("How many rows?", cnt_rt)
        assert cnt_res.answerable is True and cnt_res.omitted_findings == 0 and len(cnt_res.findings) == 1

        # 5. Email thread pagination exposes total_messages/truncated/next_offset and includes target email.
        tail_win = runtime.invoke("read_email_thread", {"document_id": sources["t4.eml"], "limit": 2})
        assert tail_win["total_messages"] == 4 and tail_win["truncated"] is True
        assert any(m["source_name"] == "t4.eml" for m in tail_win["messages"])
        head_win = runtime.invoke("read_email_thread", {"document_id": sources["t4.eml"], "limit": 2, "offset": 0})
        assert head_win["count"] == 2 and head_win["has_more"] is True and head_win["next_offset"] == 2


class _TranscriptMetaModel:
    def __init__(self, doc_id: str) -> None:
        self._doc_id = doc_id

    def complete_turn(self, messages, *, tools):
        import json
        from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn

        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("t1", "read_transcript_window", {"document_id": self._doc_id, "start_seconds": 0, "limit": 10}),
            ))
        cue = tool_msgs[0]["cues"][0]
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "At 00:01:30, Speaker 2 confirmed delivery.",
            "findings": [{
                "text": "At 00:01:30, Speaker 2 confirmed delivery.",
                "citations": [{
                    "document_id": tool_msgs[0]["document_id"],
                    "source_name": tool_msgs[0]["source_name"],
                    "location": cue["location"],
                    "source_version_id": cue["source_version_id"],
                }],
                "calculation_ids": [],
            }],
            "gaps": [],
            "competing_accounts": [],
        }))


def test_pr589_round3_xlsx_filter_ops_email_truncation_transcript_pagination_and_grounding(tmp_path):
    from case_intelligence.case_assistant import CaseAssistant

    # 1. XLSX cell-address coordinates (A1/B1 vs A2/B2) normalize to columns A/B and aggregate cleanly.
    xlsx_text = "Row 1: A1: Name | B1: Duration\nRow 2: A2: Alice | B2: 12\nRow 3: A3: Bob | B3: 18\n"
    res, receipt = query_table_rows(
        "doc-xlsx", "sheet.xlsx", xlsx_text,
        aggregate_column="Duration", aggregate_op="sum", receipt_id="calc-1",
    )
    assert res["matched_rows"] == 2
    assert res["rows"][0]["Name"] == "Alice" and res["rows"][0]["Duration"] == "12"
    assert receipt is not None and receipt.result == "30"

    # 2. Unrecognized numeric filter operators fail explicitly instead of keeping all rows.
    with pytest.raises(AssistantToolError, match="Unsupported numeric filter operator"):
        query_table_rows(
            "doc-xlsx", "sheet.xlsx", xlsx_text,
            filters={"Duration": {"gt": "15"}}, aggregate_op="count",
        )
    with pytest.raises(AssistantToolError, match="Unsupported numeric filter operator"):
        query_table_rows("doc-xlsx", "sheet.xlsx", xlsx_text, filters={"Duration": {}})

    # 3-5. Real workbench HTTP uploads for long email, 60-cue transcript, and timestamp/speaker grounding.
    long_body = ("A" * 4_500) + " final amount 98765."
    sixty_cues = "\n".join(f"[00:{i // 60:02d}:{i % 60:02d}] Speaker 1: Line {i}." for i in range(1, 61))
    grounded_tr = "[00:01:30] Speaker 2: Confirmed delivery.\n"
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test", malware_scanner=CleanScanner(), malware_scan_mode="extended")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Round 3 Rich Tools Matter")
        up = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("long.eml", _eml("long1", "Long Body", long_body), "message/rfc822")),
            ("files", ("sixty.txt", sixty_cues.encode("utf-8"), "text/plain")),
            ("files", ("delivery.txt", grounded_tr.encode("utf-8"), "text/plain")),
        ])
        assert up.status_code == 200
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        runtime = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        sources = {s["source_name"]: s["document_id"] for s in runtime.invoke("list_sources", {"limit": 20})["sources"]}

        # 3. Email body > 4,000 chars reports body_truncated=True, truncated=True, and continuation offset.
        em = runtime.invoke("read_email_thread", {"document_id": sources["long.eml"], "limit": 10})
        assert em["body_truncated"] is True and em["truncated"] is True
        span = em["messages"][0]["body_spans"][0]
        assert span["truncated"] is True and span["omitted_chars"] > 0 and span["next_char_offset"] == 4000

        # 4. 60-line transcript reports consistent count=50, total_cues=60, omitted_cues=10, and offset pagination.
        t1 = runtime.invoke("read_transcript_window", {"document_id": sources["sixty.txt"], "start_seconds": 0, "limit": 50})
        assert t1["count"] == 50 and len(t1["cues"]) == 50
        assert t1["total_cues"] == 60 and t1["omitted_cues"] == 10
        assert t1["has_more"] is True and t1["next_offset"] == 50 and t1["truncated"] is True
        t2 = runtime.invoke("read_transcript_window", {"document_id": sources["sixty.txt"], "start_seconds": 0, "limit": 50, "offset": 50})
        assert t2["count"] == 10 and len(t2["cues"]) == 10 and t2["omitted_cues"] == 0 and t2["has_more"] is False

        # 5. Transcript finding citing timestamp 00:01:30 and Speaker 2 passes numeric grounding verification.
        g_rt = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        g_res = CaseAssistant(_TranscriptMetaModel(sources["delivery.txt"])).investigate("When was delivery confirmed?", g_rt)
        assert g_res.answerable is True and g_res.omitted_findings == 0 and len(g_res.findings) == 1


def test_pr589_round4_multisheet_xlsx_and_multiline_transcript_continuations(tmp_path):
    # 1. Multi-sheet XLSX refuses silent schema merging and scopes cleanly when sheet= is specified.
    sections = [
        ("Wires, rows 1–2", "v1", "Row 1: A1: id | B1: amount\nRow 2: A2: w1 | B2: 100"),
        ("Balances, rows 1–2", "v1", "Row 1: A1: id | B1: balance\nRow 2: A2: w2 | B2: 999"),
    ]
    with pytest.raises(AssistantToolError, match="multiple worksheets"):
        query_table_rows(
            "doc-wb", "book.xlsx", filters={"id": "w"},
            aggregate_column="amount", aggregate_op="sum", sections=sections,
        )
    wires_res, wires_rcpt = query_table_rows(
        "doc-wb", "book.xlsx", filters={"id": "w"},
        aggregate_column="amount", aggregate_op="sum", sections=sections, sheet="Wires",
    )
    assert wires_res["matched_rows"] == 1 and wires_rcpt is not None and wires_rcpt.result == "100"
    bal_res, bal_rcpt = query_table_rows(
        "doc-wb", "book.xlsx", filters={"id": "w"},
        aggregate_column="balance", aggregate_op="sum", sections=sections, sheet="Balances",
    )
    assert bal_res["matched_rows"] == 1 and bal_rcpt is not None and bal_rcpt.result == "999"

    # 2. Multi-line timestamped transcript preserves continuation lines and discloses untimestamped preamble.
    multiline_tr = (
        "Header note before timestamps\n"
        "[00:00:05] Speaker 1: First sentence.\n"
        "The scheduled amount is 9876.\n"
        "[00:00:10] Speaker 2: Acknowledged.\n"
    )
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Multiline Transcript Matter")
        up = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("multi.txt", multiline_tr.encode("utf-8"), "text/plain")),
        ])
        assert up.status_code == 200
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        runtime = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        doc_id = runtime.invoke("list_sources", {"limit": 10})["sources"][0]["document_id"]
        win = runtime.invoke("read_transcript_window", {"document_id": doc_id, "start_seconds": 0, "limit": 10})
        assert win["count"] == 2
        assert "The scheduled amount is 9876." in win["cues"][0]["text"]
        assert win["untimestamped_lines_omitted"] == 1 and win["truncated"] is True


class _SpanCitationModel:
    def __init__(self, doc_id: str, cite_continuation: bool) -> None:
        self._doc_id = doc_id
        self._cite_continuation = cite_continuation

    def complete_turn(self, messages, *, tools):
        import json
        from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn

        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("t1", "read_transcript_window", {"document_id": self._doc_id, "start_seconds": 0, "limit": 50}),
            ))
        cue = tool_msgs[0]["cues"][-1]
        loc = cue["continuation_locations"][0] if self._cite_continuation else cue["location"]
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "Paid 9876.",
            "findings": [{
                "text": "Paid 9876.",
                "citations": [{
                    "document_id": tool_msgs[0]["document_id"],
                    "source_name": tool_msgs[0]["source_name"],
                    "location": loc,
                    "source_version_id": cue["source_version_id"],
                }],
                "calculation_ids": [],
            }],
            "gaps": [],
            "competing_accounts": [],
        }))


def test_pr589_round5_duplicate_coord_rejection_and_per_span_transcript_grounding(tmp_path):
    from case_intelligence.case_assistant import CaseAssistant

    # 1. Duplicate or out-of-order normalized coordinates in extracted text fail explicitly.
    injected = "Row 1: 1: id | 2: amount | 3: note\nRow 2: 1: w1 | 2: 100 | 3: x | 2: 999\n"
    with pytest.raises(AssistantToolError, match="ambiguous cell delimiters"):
        query_table_rows("doc-inj", "inj.csv", injected, aggregate_column="amount", aggregate_op="sum")
    sparse_injected = "Row 1: 1: id | 2: amount | 3: note\nRow 2: 1: w1 | 3: x | 2: 999\n"
    with pytest.raises(AssistantToolError, match="ambiguous cell delimiters"):
        query_table_rows("doc-sinj", "sinj.csv", sparse_injected, aggregate_column="amount", aggregate_op="sum")

    # 2. Real HTTP uploaded CSVs with blank cells + coordinate-like pipe text never manufacture column values.
    lines = [f"[00:00:{i:02d}] Speaker 1: Intro line {i}." for i in range(1, 20)]
    lines.append("[00:00:20] Speaker 1: Starting cross-unit cue.")
    lines.append("Paid 9876.")
    cross_tr = "\n".join(lines) + "\n"
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test", malware_scanner=CleanScanner(), malware_scan_mode="extended")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Cross Unit Span Matter")
        assert client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("cross.txt", cross_tr.encode("utf-8"), "text/plain")),
            ("files", ("sparse_inj.csv", b"id,amount,note\nw1,,x | 2: 999\n", "text/csv")),
            ("files", ("fwd_inj.csv", b"id,note,amount\nw1,x | 3: 999,\n", "text/csv")),
            ("files", ("dup_hdr.csv", b"id,amount,amount\nw1,100,999\nw2,200,888\n", "text/csv")),
        ]).status_code == 200
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        rt_init = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        src_map = {s["source_name"]: s["document_id"] for s in rt_init.invoke("list_sources", {"limit": 10})["sources"]}
        doc_id = src_map["cross.txt"]

        for csv_name in ("sparse_inj.csv", "fwd_inj.csv"):
            tbl = rt_init.invoke("query_table", {"document_id": src_map[csv_name]})
            assert tbl["matched_rows"] == 1
            assert tbl["rows"][0]["amount"] == ""
            with pytest.raises(AssistantToolError):
                rt_init.invoke("query_table", {
                    "document_id": src_map[csv_name], "aggregate_column": "amount", "aggregate_op": "sum",
                })
        # Duplicate headers fail explicitly instead of silently overwriting earlier columns.
        with pytest.raises(AssistantToolError, match="duplicate column header"):
            rt_init.invoke("query_table", {
                "document_id": src_map["dup_hdr.csv"], "aggregate_column": "amount", "aggregate_op": "sum",
            })
        # Unknown/misspelled filter columns fail explicitly instead of returning a 0-row count receipt.
        with pytest.raises(AssistantToolError, match="Filter column 'ammount' not found"):
            rt_init.invoke("query_table", {
                "document_id": src_map["sparse_inj.csv"], "filters": {"ammount": {"min": "150"}}, "aggregate_op": "count",
            })

        # Cyclic 3-email reply graph (A->B->C->A) terminates cleanly without hanging store._lock.
        assert client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("cyc_a.eml", _eml("cyc-a", "Cycle", "Msg A", in_reply_to="cyc-b"), "message/rfc822")),
            ("files", ("cyc_b.eml", _eml("cyc-b", "Cycle", "Msg B", in_reply_to="cyc-c"), "message/rfc822")),
            ("files", ("cyc_c.eml", _eml("cyc-c", "Cycle", "Msg C", in_reply_to="cyc-a"), "message/rfc822")),
        ]).status_code == 200
        src_map = {s["source_name"]: s["document_id"] for s in rt_init.invoke("list_sources", {"limit": 20})["sources"]}
        cyc_thread = rt_init.invoke("read_email_thread", {"document_id": src_map["cyc_a.eml"], "limit": 10})
        assert cyc_thread["total_messages"] == 3 and cyc_thread["count"] == 3

        wrong_rt = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        wrong_res = CaseAssistant(_SpanCitationModel(doc_id, cite_continuation=False)).investigate("How much was paid?", wrong_rt)
        assert wrong_res.answerable is False and wrong_res.omitted_findings == 1

        right_rt = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        right_res = CaseAssistant(_SpanCitationModel(doc_id, cite_continuation=True)).investigate("How much was paid?", right_rt)
        assert right_res.answerable is True and right_res.omitted_findings == 0 and len(right_res.findings) == 1

        # Locator metadata ('Page 2') in query_table rows cannot authorize fabricated numeric claims ('2').
        padded_csv = "amount\n" + ("\n" * 100) + "100\n200\n"
        assert client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("padded.csv", padded_csv.encode("utf-8"), "text/csv")),
        ]).status_code == 200
        src_map = {s["source_name"]: s["document_id"] for s in rt_init.invoke("list_sources", {"limit": 20})["sources"]}
        fab_rt = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        fab_res = CaseAssistant(_LocatorDigitModel(src_map["padded.csv"])).investigate("What was the amount?", fab_rt)
        assert fab_res.answerable is False and fab_res.omitted_findings == 1

        # Synthetic count premises ('1') cannot authorize 'The amount is 1.' even when citing the count receipt calc-1.
        cnt_fab_rt = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        cnt_fab_res = CaseAssistant(_CountPremiseLeakModel(src_map["padded.csv"])).investigate("What was the amount?", cnt_fab_rt)
        assert cnt_fab_res.answerable is False and cnt_fab_res.omitted_findings == 1

        # Numeric column headers (e.g. '2024') ground findings citing the header's originating span,
        # but NEVER ground findings citing only a later matched-row section where the header does not appear.
        padded_years = "2024,amount\n" + ("\n" * 100) + "cash,100\n"
        assert client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("years.csv", b"category,2024,2025\ncash,100,200\n", "text/csv")),
            ("files", ("padded_years.csv", padded_years.encode("utf-8"), "text/csv")),
        ]).status_code == 200
        src_map = {s["source_name"]: s["document_id"] for s in rt_init.invoke("list_sources", {"limit": 30})["sources"]}
        yr_rt = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        yr_res = CaseAssistant(_YearHeaderModel(src_map["years.csv"])).investigate("What is the 2024 total?", yr_rt)
        assert yr_res.answerable is True and yr_res.omitted_findings == 0 and len(yr_res.findings) == 1
        cross_hdr_rt = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        cross_hdr_res = CaseAssistant(_HeaderSectionLeakModel(src_map["padded_years.csv"])).investigate("What was the amount?", cross_hdr_rt)
        assert cross_hdr_res.answerable is False and cross_hdr_res.omitted_findings == 1

        # Malformed base64 inline attachment causing read_eml error surfaces incomplete_threading/error instead of truncated=False.
        bad_eml = (
            b"From: a@example.test\r\nTo: b@example.test\r\nSubject: Broken Attach\r\n"
            b"Message-ID: <broken-1@example.test>\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=\"bnd\"\r\n\r\n"
            b"--bnd\r\nContent-Type: text/html\r\n\r\n<p>See note.</p>\r\n"
            b"--bnd\r\nContent-Type: text/plain; name=\"note.txt\"\r\n"
            b"Content-Disposition: inline; filename=\"note.txt\"\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n!!!NOT_BASE64!!!\r\n--bnd--\r\n"
        )
        reply_to_bad = _eml("reply-broken", "Re: Broken Attach", "Following up.", in_reply_to="broken-1")
        assert client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("bad_attach.eml", bad_eml, "message/rfc822")),
            ("files", ("reply_bad.eml", reply_to_bad, "message/rfc822")),
        ]).status_code == 200
        src_map = {s["source_name"]: s["document_id"] for s in rt_init.invoke("list_sources", {"limit": 30})["sources"]}
        with pytest.raises(AssistantToolError, match="could not be parsed"):
            rt_init.invoke("read_email_thread", {"document_id": src_map["bad_attach.eml"], "limit": 10})
        rep_thread = rt_init.invoke("read_email_thread", {"document_id": src_map["reply_bad.eml"], "limit": 10})
        assert rep_thread["incomplete_threading"] is True and rep_thread["truncated"] is True
        assert any(e["source_name"] == "bad_attach.eml" for e in rep_thread["unthreaded_email_errors"])


class _HeaderSectionLeakModel:
    def __init__(self, doc_id: str) -> None:
        self._doc_id = doc_id

    def complete_turn(self, messages, *, tools):
        import json
        from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn

        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("q1", "query_table", {"document_id": self._doc_id}),
            ))
        row = tool_msgs[0]["rows"][0]
        assert tool_msgs[0]["header_location"] != row["_location"]
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "The amount is 2024.",
            "findings": [{
                "text": "The amount is 2024.",
                "citations": [{
                    "document_id": tool_msgs[0]["document_id"],
                    "source_name": tool_msgs[0]["source_name"],
                    "location": row["_location"],
                    "source_version_id": row["_source_version_id"],
                }],
                "calculation_ids": [],
            }],
            "gaps": [],
            "competing_accounts": [],
        }))


class _CountPremiseLeakModel:
    def __init__(self, doc_id: str) -> None:
        self._doc_id = doc_id

    def complete_turn(self, messages, *, tools):
        import json
        from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn

        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("q1", "query_table", {"document_id": self._doc_id, "aggregate_op": "count"}),
            ))
        row = tool_msgs[0]["rows"][0]
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "The amount is 1.",
            "findings": [{
                "text": "The amount is 1.",
                "citations": [{
                    "document_id": tool_msgs[0]["document_id"],
                    "source_name": tool_msgs[0]["source_name"],
                    "location": row["_location"],
                    "source_version_id": row["_source_version_id"],
                }],
                "calculation_ids": ["calc-1"],
            }],
            "gaps": [],
            "competing_accounts": [],
        }))


class _YearHeaderModel:
    def __init__(self, doc_id: str) -> None:
        self._doc_id = doc_id

    def complete_turn(self, messages, *, tools):
        import json
        from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn

        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("q1", "query_table", {
                    "document_id": self._doc_id, "aggregate_column": "2024", "aggregate_op": "sum",
                }),
            ))
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "The total for 2024 is 100.",
            "findings": [{
                "text": "The total for 2024 is 100.",
                "citations": [{
                    "document_id": tool_msgs[0]["document_id"],
                    "source_name": tool_msgs[0]["source_name"],
                    "location": tool_msgs[0]["location"],
                    "source_version_id": tool_msgs[0]["source_version_id"],
                }],
                "calculation_ids": ["calc-1"],
            }],
            "gaps": [],
            "competing_accounts": [],
        }))


class _LocatorDigitModel:
    def __init__(self, doc_id: str) -> None:
        self._doc_id = doc_id

    def complete_turn(self, messages, *, tools):
        import json
        from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn

        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("q1", "query_table", {"document_id": self._doc_id}),
            ))
        row = tool_msgs[0]["rows"][0]
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "The amount is 2.",
            "findings": [{
                "text": "The amount is 2.",
                "citations": [{
                    "document_id": tool_msgs[0]["document_id"],
                    "source_name": tool_msgs[0]["source_name"],
                    "location": row["_location"],
                    "source_version_id": row["_source_version_id"],
                }],
                "calculation_ids": [],
            }],
            "gaps": [],
            "competing_accounts": [],
        }))






