"""CI candidate isolation retains the real outgoing-history publication guard."""

from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_publication_pre_push import command, commit, repository

ROOT = Path(__file__).resolve().parents[1]


def inspect(root, *, head=None, ref="refs/pull/123/merge"):
    head = head or command(root, "git", "rev-parse", "HEAD").stdout.strip()
    return subprocess.run([sys.executable, str(ROOT / "scripts/check-publication-candidate.py"),
        "--expected-head", head, "--publication-ref", ref], cwd=root,
        capture_output=True, text=True)


def test_unrelated_fetched_branch_does_not_block_clean_candidate(tmp_path):
    root = repository(tmp_path)
    clean = command(root, "git", "rev-parse", "HEAD").stdout.strip()
    command(root, "git", "checkout", "-qb", "unrelated")
    # fixture-write: synthetic candidate content in the temporary repository.
    (root / "synthetic-residue.txt").write_text("fixture." + "internal")
    commit(root)
    command(root, "git", "checkout", "--detach", clean)
    assert inspect(root).returncode == 0


def test_removed_residue_in_candidate_ancestry_still_blocks(tmp_path):
    root = repository(tmp_path)
    # fixture-write: synthetic candidate content in the temporary repository.
    (root / "synthetic-residue.txt").write_text("fixture." + "internal")
    commit(root)
    (root / "synthetic-residue.txt").unlink()
    commit(root)
    result = inspect(root)
    assert result.returncode != 0
    assert "fixture." not in result.stderr + result.stdout


def test_unexpected_head_modified_checkout_and_invalid_ref_fail_closed(tmp_path):
    root = repository(tmp_path)
    assert inspect(root, head="f" * 40).returncode != 0
    assert inspect(root, ref="--all").returncode != 0
    # fixture-write: synthetic candidate content in the temporary repository.
    (root / "README.md").write_text("Uncommitted candidate change")
    assert inspect(root).returncode != 0


def test_annotated_tag_metadata_is_not_lost_by_peeling_to_head(tmp_path):
    root = repository(tmp_path)
    command(root, "git", "tag", "-a", "v-synthetic", "-m", "fixture." + "internal")
    assert inspect(root, ref="refs/tags/v-synthetic").returncode != 0


def test_clean_tag_and_wrong_target_tag_are_distinguished(tmp_path):
    root = repository(tmp_path)
    command(root, "git", "tag", "-a", "v-synthetic", "-m", "Synthetic release")
    assert inspect(root, ref="refs/tags/v-synthetic").returncode == 0
    # fixture-write: synthetic candidate content in the temporary repository.
    (root / "README.md").write_text("Another synthetic candidate")
    commit(root)
    assert inspect(root, ref="refs/tags/v-synthetic").returncode != 0


def test_branch_ref_metadata_is_preserved_and_inspected(tmp_path):
    root = repository(tmp_path)
    assert inspect(root, ref="refs/heads/synthetic-clean").returncode == 0
    assert inspect(root, ref="refs/heads/fixture." + "internal").returncode != 0


def test_deleted_ref_events_skip_candidate_inspection():
    workflow = (ROOT / ".github/workflows/quality-gates.yml").read_text()
    scan = workflow.split("- name: Scan exact candidate tree and complete reachable history", 1)[1].split("- name:", 1)[0]
    assert "if: ${{ !github.event.deleted }}" in scan


def test_pull_request_workflow_passes_actual_source_branch_metadata():
    workflow = (ROOT / ".github/workflows/quality-gates.yml").read_text()
    assert "PUBLICATION_REF: ${{ github.head_ref && format('refs/heads/{0}', github.head_ref) || github.ref }}" in workflow


@pytest.mark.parametrize('entrypoint', ['candidate', 'pre-push'])
@pytest.mark.parametrize('payload', ['other-trailer', 'personal-trailer', 'body'])
def test_candidate_and_pre_push_share_trailer_email_policy(tmp_path, entrypoint, payload):
    from tests.test_publication_pre_push import inspect as inspect_push

    root = repository(tmp_path)
    address = 'person@' + 'synthetic.invalid'
    if payload == 'personal-trailer':
        address = 'ne' + 'il@' + 'ne' + 'ilofneils.com'
    message = 'Synthetic\n\n'
    message += address if payload == 'body' else 'Signed-off-by: Synthetic Person <' + address + '>'
    assert command(root, 'git', 'commit', '--allow-empty', '-qm', message).returncode == 0
    result = inspect(root) if entrypoint == 'candidate' else inspect_push(root)
    assert result.returncode == (0 if payload == 'other-trailer' else 1)
