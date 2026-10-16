"""Exercise the real hook against isolated repositories; never contact a remote."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def command(root, *args, **kwargs):
    return subprocess.run(args, cwd=root, capture_output=True, text=True, **kwargs)


def repository(tmp_path, object_format="sha1"):
    root = tmp_path / "source"
    root.mkdir()
    for args in [("init", "-q", "--object-format=" + object_format), ("config", "user.name", "Synthetic Author"),
                 ("config", "user.email", "author@example.test")]:
        assert command(root, "git", *args).returncode == 0
    (root / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/publication-check.py", root / "scripts/publication-check.py")
    (root / "README.md").write_text("Synthetic example\n")
    commit(root)
    return root


def commit(root):
    assert command(root, "git", "add", ".").returncode == 0
    assert command(root, "git", "commit", "-qm", "Synthetic change").returncode == 0
    return command(root, "git", "rev-parse", "HEAD").stdout.strip()


def inspect(root, oid=None, ref="refs/heads/main"):
    oid = oid or command(root, "git", "rev-parse", "HEAD").stdout.strip()
    return command(root, "python3", str(ROOT / "scripts/pre-push-publication.py"),
                   input=f"refs/heads/main {oid} {ref} {'0' * 40}\n")


def test_clean_outgoing_history_passes(tmp_path):
    assert inspect(repository(tmp_path)).returncode == 0


def test_removed_private_residue_still_blocks_history(tmp_path):
    root = repository(tmp_path)
    (root / "private.txt").write_text("host." + "internal")
    commit(root)
    (root / "private.txt").unlink()
    commit(root)
    result = inspect(root)
    assert result.returncode != 0
    assert "host." not in result.stderr


def test_all_outgoing_refs_are_checked(tmp_path):
    root = repository(tmp_path)
    clean = command(root, "git", "rev-parse", "HEAD").stdout.strip()
    (root / "private.txt").write_text("host." + "internal")
    unsafe = commit(root)
    result = command(root, "python3", str(ROOT / "scripts/pre-push-publication.py"),
        input=f"main {clean} refs/heads/main {'0'*40}\nother {unsafe} refs/heads/other {'0'*40}\n")
    assert result.returncode != 0


def test_required_private_deny_file_fails_closed(tmp_path):
    root = repository(tmp_path)
    command(root, "git", "config", f"{LEGACY_STEM}.requirePrivatePublicationCheck", "true")
    assert inspect(root).returncode != 0
    deny = tmp_path / "deny.txt"
    deny.write_text("synthetic-disallowed-marker\n")
    deny.chmod(0o600)
    command(root, "git", "config", f"{LEGACY_STEM}.publicationDenyFile", str(deny))
    assert inspect(root).returncode == 0
    (root / "private.txt").write_text("synthetic-disallowed-marker")
    commit(root)
    assert inspect(root).returncode != 0


def test_tag_metadata_is_checked(tmp_path):
    root = repository(tmp_path)
    command(root, "git", "tag", "-a", "v-test", "-m", "host." + "internal")
    oid = command(root, "git", "rev-parse", "v-test").stdout.strip()
    assert inspect(root, oid, "refs/tags/v-test").returncode != 0


def test_unrelated_local_history_is_not_exported(tmp_path):
    root = repository(tmp_path)
    clean = command(root, "git", "rev-parse", "HEAD").stdout.strip()
    command(root, "git", "checkout", "-qb", "private-local")
    (root / "private.txt").write_text("host." + "internal")
    commit(root)
    assert inspect(root, clean).returncode == 0


def test_nested_tag_metadata_is_checked_even_without_inner_ref(tmp_path):
    root = repository(tmp_path)
    command(root, "git", "tag", "-a", "inner", "-m", "host." + "internal")
    command(root, "git", "tag", "-a", "outer", "inner", "-m", "Synthetic release")
    command(root, "git", "tag", "-d", "inner")
    oid = command(root, "git", "rev-parse", "outer").stdout.strip()
    assert inspect(root, oid, "refs/tags/outer").returncode != 0


def test_sha256_repository_uses_matching_inspection_format(tmp_path):
    root = repository(tmp_path, "sha256")
    boundary = command(root, "git", "rev-parse", "HEAD").stdout.strip()
    command(root, "git", "config", f"{LEGACY_STEM}.publicBaselineIdentity",
            boundary + "\tSynthetic Author\t12345+fixture@users.noreply.github.com")
    command(root, "git", "config", f"{LEGACY_STEM}.publicMergeCommit", boundary)
    assert inspect(root).returncode == 0


def test_malformed_required_private_check_setting_blocks(tmp_path):
    root = repository(tmp_path)
    command(root, "git", "config", f"{LEGACY_STEM}.requirePrivatePublicationCheck", "invalid-setting")
    assert inspect(root).returncode != 0


def test_unrelated_baseline_does_not_fetch_or_block_orphan_history(tmp_path):
    root = repository(tmp_path)
    (root / "private.txt").write_text("host." + "internal")
    boundary = commit(root)
    command(root, "git", "config", f"{LEGACY_STEM}.publicBaselineIdentity",
            boundary + "\tSynthetic Author\t12345+fixture@users.noreply.github.com")
    command(root, "git", "checkout", "--orphan", "public-other")
    (root / "private.txt").unlink()
    commit(root)
    assert inspect(root).returncode == 0


def test_tag_target_hash_is_structural_but_tag_message_still_scans(tmp_path):
    root = repository(tmp_path)
    oid = command(root, "git", "rev-parse", "HEAD").stdout.strip()
    metadata = command(root, "git", "show", "-s", "--format=%an %ae %cn %ce %B").stdout
    command(root, "git", "tag", "-a", "v-test", "-m", "Synthetic release")
    tag_metadata = "\n".join(command(root, "git", "cat-file", "tag", "v-test").stdout.splitlines()[2:])
    content = (root / "scripts/publication-check.py").read_text() + metadata + tag_metadata + "refs/tags/v-test"
    term = next(oid[i:i+3] for i in range(len(oid)-2) if oid[i:i+3] not in content.lower())
    deny = tmp_path / "deny.txt"
    deny.write_text(term + "\n")
    deny.chmod(0o600)
    command(root, "git", "config", f"{LEGACY_STEM}.publicationDenyFile", str(deny))
    tag_oid = command(root, "git", "rev-parse", "v-test").stdout.strip()
    assert inspect(root, tag_oid, "refs/tags/v-test").returncode == 0
    command(root, "git", "tag", "-f", "-a", "v-test", "-m", term)
    tag_oid = command(root, "git", "rev-parse", "v-test").stdout.strip()
    assert inspect(root, tag_oid, "refs/tags/v-test").returncode != 0


def test_installed_hook_does_not_execute_candidate_scanner(tmp_path):
    import sys
    root = repository(tmp_path)
    destination = tmp_path / "trusted-hook"
    result = command(root, sys.executable, str(ROOT / "scripts/install-publication-hook.py"), str(destination))
    assert result.returncode == 0, result.stderr
    marker = root / "executed"
    # Keep the malicious fixture independent of the test runner's home path;
    # that path is itself forbidden publication content.
    (root / "scripts/publication-check.py").write_text("from pathlib import Path\nPath('executed').touch()\n")
    oid = commit(root)
    result = command(root, str(destination / "pre-push"), input=f"refs/heads/main {oid} refs/heads/main {'0' * 40}\n")
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_installer_rejects_external_interpreter_link_into_checkout(tmp_path):
    import sys
    root = repository(tmp_path)
    target = root / "candidate-python"
    shutil.copyfile(sys.executable, target)
    target.chmod(0o700)
    interpreter = tmp_path / "external-python"
    interpreter.symlink_to(target)
    destination = tmp_path / "trusted-hook"
    result = command(root, str(interpreter), str(ROOT / "scripts/install-publication-hook.py"), str(destination))
    assert result.returncode != 0
    assert not destination.exists()


def test_installed_scanner_ignores_candidate_pythonpath(tmp_path):
    import os
    import sys
    root = repository(tmp_path)
    destination = tmp_path / "trusted-hook"
    result = command(root, sys.executable, str(ROOT / "scripts/install-publication-hook.py"), str(destination))
    assert result.returncode == 0, result.stderr
    marker = tmp_path / "executed"
    (root / "sitecustomize.py").write_text("import os\nfrom pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\nos._exit(0)\n")
    (root / "private.txt").write_text("host." + "internal")
    oid = commit(root)
    environment = dict(os.environ, PYTHONPATH=str(root))
    result = command(root, str(destination / "pre-push"), env=environment,
                     input=f"refs/heads/main {oid} refs/heads/main {'0' * 40}\n")
    assert not marker.exists()
    assert result.returncode != 0
