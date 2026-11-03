"""Synthetic rendering checks for the reusable coverage report panel."""

from html.parser import HTMLParser
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest


TEMPLATES = Path(__file__).resolve().parents[1] / "src/case_intelligence/templates"


class PanelParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details_depth = 0
        self.outside_details = []
        self.headings = 0
        self.summaries = 0
        self.tables = 0

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.details_depth += 1
        elif tag == "summary":
            self.summaries += 1
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.headings += 1
        elif tag == "table":
            self.tables += 1

    def handle_endtag(self, tag):
        if tag == "details":
            self.details_depth -= 1

    def handle_data(self, data):
        if not self.details_depth:
            self.outside_details.append(data)


@pytest.fixture
def render_report():
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(("html",)),
    )
    template = environment.from_string(
        '{% from "workbench_coverage_report_macros.html" import coverage_report %}'
        "{{ coverage_report(report) }}"
    )
    return lambda report: template.render(report=report)


def example_reports():
    return [
        {
            "matter_scope": {"label": "Matter M-104"},
            "filters": [],
            "search_lanes": ["Lexical", "Semantic", "Metadata"],
            "catalog_source_count": 24,
            "eligible_source_count": 24,
            "searched_source_count": 24,
            "model_read_full_count": 24,
            "model_read_partial_count": 0,
            "model_read_skipped_count": 0,
            "pending_source_count": 0,
            "changed_during_query_count": 0,
            "failed_source_count": 0,
            "limitations": ["PDF extraction and OCR are bounded processes."],
            "record_type_coverage": [
                {"label": "dated-event", "attempted": 24, "produced": 3,
                 "skipped": 0, "failed": 0, "schema_version": "1"}
            ],
            "measured_recall": None,
        },
        {
            "matter_scope": {"label": "Matter M-208"},
            "filters": ["messages dated January through March", "<script>alert(1)</script>"],
            "search_lanes": ["Lexical", "Semantic"],
            "catalog_source_count": 1240,
            "eligible_source_count": 1200,
            "searched_source_count": 1180,
            "model_read_full_count": 960,
            "model_read_partial_count": 220,
            "model_read_partial_reasons": [{"label": "read budget", "count": 220}],
            "model_read_skipped_count": 20,
            "model_read_skipped_reasons": [{"label": "processing pending", "count": 20}],
            "pending_source_count": 20,
            "changed_during_query_count": 2,
            "failed_source_count": 1,
            "failed_source_reasons": [{"label": "extraction error", "count": 1}],
            "limitations": ["Results may omit material from those sources."],
            "record_type_coverage": [
                {"label": "quoted-statement", "attempted": 1200, "produced": 11,
                 "skipped": 20, "failed": 1, "schema_version": "2"}
            ],
            "measured_recall": {
                "label": "quoted-statement extraction", "sample_size": 150,
                "estimate": "92%", "interval": "86% to 96%", "interval_level": "95%",
                "measurement_date": "2026-10-15", "method": "elusion sample",
            },
        },
        {
            "matter_scope": {"label": "selected source set S-12"},
            "filters": ["PDFs"],
            "search_lanes": ["Lexical", "Metadata"],
            "catalog_source_count": 87,
            "eligible_source_count": 87,
            "searched_source_count": 87,
            "model_read_full_count": 78,
            "model_read_partial_count": 0,
            "model_read_skipped_count": 9,
            "model_read_skipped_reasons": [{"label": "no usable text", "count": 9}],
            "pending_source_count": 0,
            "changed_during_query_count": 0,
            "failed_source_count": 6,
            "failed_source_reasons": [{"label": "OCR failure", "count": 6}],
            "ocr": {"attempted": 31, "returned": 22, "failed": 6, "skipped": 3,
                    "skipped_reasons": [{"label": "page cap", "count": 3}]},
            "limitations": ["The answer may omit content that OCR did not return.",
                            "Review the original source when the distinction matters."],
            "record_type_coverage": [
                {"label": "timeline entry", "attempted": 87, "produced": 2,
                 "skipped": 0, "failed": 6, "schema_version": "1"}
            ],
            "measured_recall": None,
        },
    ]


@pytest.mark.parametrize("report", example_reports())
def test_summary_orders_scope_activity_and_limitations_and_exposes_nonzero_counts(render_report, report):
    html = render_report(report)
    parser = PanelParser()
    parser.feed(html)
    summary = " ".join(" ".join(parser.outside_details).split())
    assert summary.index("Scope:") < summary.index("Activity:") < summary.index("Limitations:")
    for field in (
        "model_read_partial_count", "model_read_skipped_count", "pending_source_count",
        "changed_during_query_count", "failed_source_count",
    ):
        if report.get(field, 0):
            assert str(report[field]) in summary
    if report.get("ocr"):
        for field in ("failed", "skipped"):
            assert str(report["ocr"][field]) in summary


@pytest.mark.parametrize("report", example_reports())
def test_wording_denominators_reasons_and_zero_detail_counts(render_report, report):
    html = render_report(report)
    lowered = html.lower()
    text = " ".join(re.sub(r"<[^>]+>", "", html).split())
    for forbidden in ("complete", "comprehensive", "all relevant", "cleared"):
        assert forbidden not in lowered
    assert "candidate for attorney review" in lowered
    assert f'searched {report["searched_source_count"]:,} of {report["eligible_source_count"]:,} eligible sources' in text
    assert "0 of" in html
    for group in report.get("model_read_skipped_reasons", []) + report.get("failed_source_reasons", []):
        assert group["label"] in html
    if report["measured_recall"]:
        recall = report["measured_recall"]
        assert all(str(recall[key]) in html for key in ("sample_size", "interval", "measurement_date"))
    else:
        assert "Measured recall: not measured" in text


def test_panel_accessibility_and_filter_escaping(render_report):
    html = render_report(example_reports()[1])
    parser = PanelParser()
    parser.feed(html)
    assert parser.headings == 1
    assert parser.summaries == 1
    assert parser.tables == 0
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>" not in html
