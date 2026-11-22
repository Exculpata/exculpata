"""Synthetic proving tests for Slice 6 Case Assistant UI polish and live stage labels."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
from fastapi.testclient import TestClient

from case_intelligence.case_assistant import CaseAssistant
from case_intelligence.case_assistant_tools import MatterToolRuntime
from case_intelligence.workbench import create_workbench_app
from tests.test_case_assistant import _ScriptedStockModel
from tests.test_case_assistant_tools import _stock_backend
from tests.test_milestone_a_workbench import _answer_and_wait, _create_matter
from tests.test_workbench_assistant import _ScriptedWorkbenchAssistantClient

STATIC = Path(__file__).resolve().parents[1] / "src/case_intelligence/static"


def test_case_assistant_emits_human_readable_live_stage_labels():
    stages: list[tuple[str, str]] = []
    runtime = MatterToolRuntime("matter-stock", "defender-1", _stock_backend())
    CaseAssistant(_ScriptedStockModel()).investigate(
        "Did controlled accounts exceed 10%?", runtime,
        stage_callback=lambda key, msg: stages.append((key, msg)),
    )
    messages = [msg for _, msg in stages]
    assert any("discovery inventory" in m.casefold() for m in messages)
    assert any("searching discovery" in m.casefold() for m in messages)
    assert any("reading full source" in m.casefold() for m in messages)
    assert any("exact calculation" in m.casefold() for m in messages)
    assert not any("Running list_sources" in m for m in messages)


def test_proposal_cards_and_trace_render_accessible_markup_css_and_valid_js(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")
    app = create_workbench_app(tmp_path / "runtime", generator=_ScriptedWorkbenchAssistantClient(), auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic UI Polish Matter")
        upload = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("buyback-filing.txt", b"Orbel Spindle Works buyback effective 2024-09-01 outstanding shares 800000.\n", "text/plain")),
            ("files", ("stmt-stk0.txt", b"Position date=2024-09-30 account=stk-0 shares=50000.\n", "text/plain")),
            ("files", ("stmt-stk1.txt", b"Position date=2024-09-30 account=stk-1 shares=40000.\n", "text/plain")),
        ])
        assert upload.status_code == 200
        conv_id = re.search(r'name="conversation" value="([^"]+)"', client.get(f"/matters/{slug}").text).group(1)
        _, page = _answer_and_wait(client, slug, conv_id, "Did controlled accounts exceed 10%?")
        for html in (page.text, client.get(f"/matters/{slug}/assistant", params={"conversation": conv_id}).text):
            assert "data-assistant-work-trace" in html
            assert "data-assistant-proposal-form" in html
            assert "data-assistant-proposal-status" in html
            assert "assistant-proposal-badge" in html

    css = (STATIC / "case-intelligence.css").read_text(encoding="utf-8")
    assert ".assistant-work-trace" in css and ".assistant-proposal-card" in css
    js_path = STATIC / "case-intelligence.js"
    assert "data-assistant-proposal-form" in js_path.read_text(encoding="utf-8")
    node = shutil.which("node")
    if node:
        check = subprocess.run([node, "--check", str(js_path)], capture_output=True, text=True, check=False)
        assert check.returncode == 0, check.stderr
