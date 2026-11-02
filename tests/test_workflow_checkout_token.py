"""Checkout must use its default token or an explicitly nonempty input."""

from pathlib import Path

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


def empty_checkout_tokens(workflow):
    failures = []
    for job_name, job in workflow["jobs"].items():
        for index, step in enumerate(job.get("steps", []), start=1):
            if not step.get("uses", "").startswith("actions/checkout@"):
                continue
            inputs = step.get("with") or {}
            if "token" in inputs:
                token = inputs["token"]
                if token is None or (isinstance(token, str) and not token.strip()):
                    failures.append(f"{job_name} step {index}: omit the empty checkout token input")
    return failures


@pytest.mark.parametrize("path", sorted(WORKFLOWS.iterdir()), ids=lambda path: path.name)
def test_workflow_checkout_token_is_not_empty(path):
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert empty_checkout_tokens(workflow) == [], path.name


@pytest.mark.parametrize("inputs", ['token: ""', 'token: "  "', 'token: "\\t"', "token:", "token: |\n          \n"])
def test_empty_checkout_token_is_rejected(inputs):
    workflow = yaml.safe_load(
        "jobs:\n  synthetic:\n    steps:\n      - uses: actions/checkout@v4\n"
        f"        with:\n          {inputs}\n"
    )
    assert empty_checkout_tokens(workflow) == [
        "synthetic step 1: omit the empty checkout token input"
    ]


@pytest.mark.parametrize("step", [
    {"uses": "actions/checkout@v4"},
    {"uses": "actions/checkout@v4", "with": {"persist-credentials": False}},
    {"uses": "actions/checkout@v4", "with": {"token": "${{ github.token }}"}},
    {"uses": "actions/setup-python@v5", "with": {"token": ""}},
    {"run": "echo synthetic"},
])
def test_default_and_nonempty_checkout_tokens_are_allowed(step):
    assert empty_checkout_tokens({"jobs": {"synthetic": {"steps": [step]}}}) == []
