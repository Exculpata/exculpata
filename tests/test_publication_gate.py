from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from pypdf import PdfWriter


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "publication_check", ROOT / "scripts" / "publication-check.py"
)
assert SPEC is not None and SPEC.loader is not None
publication = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = publication
SPEC.loader.exec_module(publication)


def test_publication_gate_flags_private_networks_keys_and_personal_paths() -> None:
    data = (
        b"service=" + b"10." + b"23.45.67\n"
        b"path=/" + b"home/specific.operator/runtime\n"
        b"-----BEGIN " + b"PRIVATE KEY-----\n"
    )
    rules = {item.rule for item in publication._scan_bytes(data, location="fixture", deny=())}
    assert rules == {
        "private-network-address",
        "personal-home-path",
        "private-key-material",
    }


def test_publication_gate_keeps_the_previous_fixture_home_owner() -> None:
    from case_intelligence.legacy_names import LEGACY_STEM

    retained = b"/home/" + LEGACY_STEM.encode() + b"/runtime"
    assert publication._scan_bytes(retained, location="fixture", deny=()) == []
    assert publication._scan_bytes(b"/home/" + b"specific.operator/runtime", location="fixture", deny=())


def test_publication_gate_allows_loopback_and_documentation_addresses() -> None:
    data = (
        b"127.0.0.1 ::1 192.0.2.10 198.51.100.8 203.0.113.4 2001:db8::8 "
        b"reviewer@example.test /home/exculpata/runtime"
    )
    assert publication._scan_bytes(data, location="fixture", deny=()) == []


def test_publication_gate_flags_generic_identity_and_secret_shapes() -> None:
    data = (
        b"endpoint=https://review." + b"agency." + b"internal/api\n"
        b"operator=C:\\" + b"Users\\Specific.Operator\\workspace\n"
        b"network=fd" + b"00::42\n"
        b"Authorization: Bearer " + b"actual-looking-token-value-123456789\n"
        b"api_key=" + b"actual-looking-api-key-value-12345\n"
        b"-----BEGIN " + b"CERTIFICATE-----\n"
    )
    rules = {item.rule for item in publication._scan_bytes(data, location="fixture", deny=())}
    assert rules == {
        "bearer-token",
        "certificate-material",
        "internal-fqdn",
        "literal-secret-shaped-value",
        "personal-home-path",
        "private-network-address",
    }


def test_publication_gate_allows_placeholders_and_code_references() -> None:
    data = (
        b"token=placeholder-token-value-123456789\n"
        b"secret=/run/exculpata-secrets/proxy-secret\n"
        b"secret = self.settings.proxy_secret\n"
    )
    assert publication._scan_bytes(data, location="fixture", deny=()) == []


def test_publication_gate_rejects_runtime_artifacts_and_symlinks(tmp_path) -> None:
    # fixture-write: synthetic publication scanner input in the temporary repository.
    (tmp_path / "safe.txt").write_text("synthetic fixture", encoding="utf-8")
    # fixture-write: synthetic publication scanner input in the temporary repository.
    (tmp_path / "case.sqlite3").write_bytes(b"not-a-real-database")
    (tmp_path / "link").symlink_to(tmp_path / "safe.txt")
    findings = {(item.location, item.rule) for item in publication.scan_tree(tmp_path, ())}
    assert ("case.sqlite3", "runtime-or-credential-file") in findings
    assert ("link", "symbolic-link") in findings


def test_publication_gate_rejects_extended_credential_suffixes(tmp_path) -> None:
    for suffix in (".cer", ".crt", ".der", ".jks", ".keystore"):
        # fixture-write: synthetic publication scanner input in the temporary repository.
        (tmp_path / f"synthetic{suffix}").write_bytes(b"not-a-real-credential")
    rules = [item.rule for item in publication.scan_tree(tmp_path, ())]
    assert rules.count("runtime-or-credential-file") == 5


def test_every_container_context_excludes_private_operator_state() -> None:
    contexts = (
        ROOT / ".dockerignore",
        ROOT / "services" / "transcription" / ".dockerignore",
        ROOT / "deploy" / "kerberos-proxy" / ".dockerignore",
        ROOT / "deploy" / "model-stager" / ".dockerignore",
    )
    required_markers = (
        ".env",
        "secrets",
        "certs",
        "*.keytab",
        "*.keystore",
        "*.sqlite3",
        "*.log",
        "*.zip",
        "runtime",
        "state",
        "backups",
    )
    for dockerignore in contexts:
        rules = dockerignore.read_text(encoding="utf-8")
        for marker in required_markers:
            assert marker in rules, f"{dockerignore} does not exclude {marker}"


def test_operator_deny_terms_are_not_echoed_in_findings() -> None:
    hidden = b"private-organization-marker"
    findings = publication._scan_bytes(
        b"prefix private-organization-marker suffix",
        location="fixture",
        deny=(hidden,),
    )
    assert findings == [publication.Finding("fixture", "operator-deny-term")]
    assert hidden.decode() not in repr(findings)


def test_public_repository_identity_adjudication_is_exact_and_context_bound(
    tmp_path,
) -> None:
    public_name = "fixture-maintainer"
    public_email = "12345+fixture-maintainer@users.noreply.github.com"
    clone_url = "https://github.com/fixture-maintainer/exculpata-fixture.git"
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.name", public_name], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", public_email], cwd=tmp_path, check=True
    )
    readme = tmp_path / "README.md"
    # fixture-write: synthetic publication scanner input in the temporary repository.
    readme.write_text(f"git clone {clone_url} exculpata\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "synthetic public baseline"],
        cwd=tmp_path,
        check=True,
    )
    deny = (public_name.encode(),)

    assert publication.Finding(
        "README.md", "operator-deny-term"
    ) in publication.scan_tree(tmp_path, deny)
    assert publication.Finding(
        "git-metadata", "operator-deny-term"
    ) in publication.scan_history(tmp_path, deny)
    assert all(
        item.rule != "operator-deny-term"
        for item in publication.scan_tree(
            tmp_path, deny, public_clone_urls=(clone_url.encode(),)
        )
    )
    assert all(
        item.rule != "operator-deny-term"
        for item in publication.scan_history(
            tmp_path,
            deny,
            public_clone_urls=(clone_url.encode(),),
            public_git_identities=(
                (public_name.encode(), public_email.encode()),
            ),
        )
    )

    private_context = tmp_path / "operator-notes.txt"
    # fixture-write: synthetic publication scanner input in the temporary repository.
    private_context.write_text(public_name, encoding="utf-8")
    assert publication.Finding(
        "operator-notes.txt", "operator-deny-term"
    ) in publication.scan_tree(
        tmp_path, deny, public_clone_urls=(clone_url.encode(),)
    )
    private_context.unlink()
    # fixture-write: synthetic publication scanner input in the temporary repository.
    readme.write_text(
        f"git clone {clone_url}.lookalike exculpata\n", encoding="utf-8"
    )
    assert publication.Finding(
        "README.md", "operator-deny-term"
    ) in publication.scan_tree(
        tmp_path, deny, public_clone_urls=(clone_url.encode(),)
    )


def test_public_baseline_identity_adjudication_stops_at_exact_commit(tmp_path) -> None:
    baseline_name = "Synthetic Maintainers"
    baseline_email = "12345+fixture-maintainer@users.noreply.github.com"
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.name", baseline_name], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", baseline_email], cwd=tmp_path, check=True
    )
    record = tmp_path / "README.md"
    # fixture-write: synthetic publication scanner input in the temporary repository.
    record.write_text("synthetic baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "synthetic accepted baseline"],
        cwd=tmp_path,
        check=True,
    )
    boundary = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    exception = ((boundary, baseline_name.encode(), baseline_email.encode()),)
    deny = (baseline_name.encode(),)

    assert all(
        item.rule != "operator-deny-term"
        for item in publication.scan_history(
            tmp_path,
            deny,
            baseline_public_git_identities=exception,
        )
    )

    # fixture-write: synthetic publication scanner input in the temporary repository.
    record.write_text("synthetic successor\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "synthetic successor"],
        cwd=tmp_path,
        check=True,
    )
    assert publication.Finding(
        "git-metadata", "operator-deny-term"
    ) in publication.scan_history(
        tmp_path,
        deny,
        baseline_public_git_identities=exception,
    )


def test_publication_gate_scans_pdf_metadata_and_archive_members(tmp_path) -> None:
    pdf = tmp_path / "synthetic.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Author": "reviewer@" + "private.invalid"})
    # fixture-write: synthetic PDF metadata for publication inspection.
    with pdf.open("wb") as handle:
        # fixture-write: synthetic PDF bytes in the temporary fixture.
        writer.write(handle)

    archive = tmp_path / "synthetic.docx"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        # fixture-write: synthetic archive member for publication inspection.
        output.writestr("word/document.xml", "server " + "10." + "42.19.7")

    findings = {(item.location, item.rule) for item in publication.scan_tree(tmp_path, ())}
    assert ("synthetic.pdf#pdf-content", "non-example-email-address") in findings
    assert ("synthetic.docx!member[1]", "private-network-address") in findings


def test_archive_member_names_are_scanned_without_echoing_private_values() -> None:
    payload = __import__("io").BytesIO()
    private_name = "records/" + "10." + "91.8.7" + "/notes.txt"
    with zipfile.ZipFile(payload, "w") as output:
        # fixture-write: synthetic archive member for publication inspection.
        output.writestr(private_name, "synthetic")

    findings = publication._scan_archive(
        payload.getvalue(), location="fixture.zip", deny=()
    )
    assert publication.Finding(
        "fixture.zip!member[1]-name", "private-network-address"
    ) in findings
    assert private_name not in repr(findings)


def test_media_metadata_is_scanned(monkeypatch) -> None:
    metadata = {
        "format": {
            "tags": {"comment": "reviewer@" + "private.invalid"},
        }
    }
    monkeypatch.setattr(publication.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        publication.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(metadata).encode("utf-8")
        ),
    )

    findings = publication._scan_media(
        b"synthetic-media", name="fixture.wav", location="fixture.wav", deny=()
    )
    assert findings == [
        publication.Finding(
            "fixture.wav#media-metadata", "non-example-email-address"
        )
    ]


def test_unsupported_media_root_preserves_content_findings(monkeypatch) -> None:
    metadata = [{"comment": "reviewer@" + "private.invalid synthetic-denied-marker"}]
    monkeypatch.setattr(publication.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        publication.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(metadata).encode("utf-8")
        ),
    )
    findings = publication._scan_content(
        b"synthetic-media", name="fixture.wav", location="fixture.wav",
        deny=(b"synthetic-denied-marker",),
    )
    assert any("unsupported-media-metadata" in item.rule and "_scan_media" in item.rule
               for item in findings)
    assert publication.Finding("fixture.wav#media-metadata", "non-example-email-address") in findings
    assert publication.Finding("fixture.wav#media-metadata", "operator-deny-term") in findings


def test_history_scan_finds_a_forbidden_blob_after_it_is_deleted(tmp_path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Synthetic Maintainer"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "maintainer@example.com"],
        cwd=tmp_path,
        check=True,
    )
    residue = tmp_path / "old-config.txt"
    # fixture-write: synthetic publication scanner input in the temporary repository.
    residue.write_text("service=" + "10." + "77.8.9", encoding="utf-8")
    subprocess.run(["git", "add", "old-config.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "synthetic first"], cwd=tmp_path, check=True)
    residue.unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove fixture"], cwd=tmp_path, check=True)

    findings = publication.scan_history(tmp_path, ())
    assert any(item.rule == "private-network-address" for item in findings)
    assert any("old-config.txt" in item.location for item in findings)


def test_history_scan_checks_author_and_committer_identity(tmp_path, monkeypatch) -> None:
    import hashlib
    monkeypatch.setattr(publication, "MAINTAINER_EMAIL_DIGESTS", frozenset({
        hashlib.sha256(b"maintainer@" + b"agency.example.org").hexdigest(),
    }))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Synthetic Maintainer"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "maintainer@" + "agency.example.org",
        ],
        cwd=tmp_path,
        check=True,
    )
    # fixture-write: synthetic publication scanner input in the temporary repository.
    (tmp_path / "README.md").write_text("synthetic\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "synthetic"], cwd=tmp_path, check=True)

    assert publication.Finding(
        "git-metadata", "maintainer-email-address"
    ) in publication.scan_history(tmp_path, ())


def test_reviewed_merge_identity_exception_is_commit_and_context_bound(tmp_path) -> None:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Synthetic Maintainer")
    git("config", "user.email", "maintainer@example.com")
    name = b"fixture-private-name"
    identity = ((name, b"12345+fixture-private-name@users.noreply.github.com"),)
    git("commit", "--allow-empty", "-qm", "Merge pull request #1 from fixture-private-name/topic")
    reviewed = git("rev-parse", "HEAD")
    def findings(*commits):
        return publication.scan_history(tmp_path, (name,), public_git_identities=identity,
                                        public_merge_commits=commits)
    assert any(f.rule == "operator-deny-term" for f in findings())
    assert not findings(reviewed)
    assert any(f.rule == "operator-deny-term" for f in publication.scan_history(
        tmp_path, (name + b"/topic",), public_git_identities=identity, public_merge_commits=(reviewed,)))
    git("commit", "--allow-empty", "-qm", "Merge pull request #2 from fixture-private-name/topic")
    assert any(f.rule == "operator-deny-term" for f in findings(reviewed))
    second = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "Merge pull request #3 from fixture-private-name/fixture-private-name\n\nfixture-private-name")
    third = git("rev-parse", "HEAD")
    assert any(f.rule == "operator-deny-term" for f in findings(reviewed, second, third))


def test_github_merge_service_address_is_public_but_other_addresses_still_block():
    service = b"noreply@" + b"github.com"
    assert publication._scan_bytes(service, location="git-metadata", deny=()) == []
    private = b"personal@" + b"github.com"
    assert publication._scan_bytes(service + b" " + private, location="git-metadata", deny=())
    assert publication._scan_bytes(service, location="git-metadata", deny=(b"github",))


def test_public_bot_identities_and_dependabot_signoff_are_exact():
    dependabot = b"49699333+dependabot[bot]@users.noreply.github.com"
    web_flow = b"noreply@" + b"github.com"
    signoff = b"Signed-off-by: dependabot[bot] <support@" + b"github.com>"
    personal = b"ada@" + b"personal.example"
    support = b"support@" + b"github.com"
    private_bot = b"49699333+dependabot[bot]@" + b"private.example"
    email = publication.Finding("git-metadata", "non-example-email-address")
    denied = publication.Finding("git-metadata", "operator-deny-term")

    cloud_agent = b"cursoragent@" + b"cursor.com"
    other_cursor = b"other@" + b"cursor.com"
    assert publication._scan_bytes(dependabot, location="git-metadata", deny=()) == []
    assert publication._scan_bytes(web_flow, location="git-metadata", deny=()) == []
    assert publication._scan_bytes(cloud_agent, location="git-metadata", deny=()) == []
    assert email in publication._scan_bytes(other_cursor, location="git-metadata", deny=())
    assert publication._scan_bytes(b"Bump\n\n" + signoff + b"\n", location="git-metadata", deny=()) == []
    assert publication._scan_bytes(signoff + b"\r", location="git-metadata", deny=()) == []
    assert email in publication._scan_bytes(personal, location="git-metadata", deny=())
    assert email in publication._scan_bytes(dependabot + b"\n" + personal, location="git-metadata", deny=())
    assert email in publication._scan_bytes(support, location="git-metadata", deny=())
    assert email in publication._scan_bytes(b"Contact " + support + b" for help", location="git-metadata", deny=())
    assert email in publication._scan_bytes(signoff + b" thanks", location="git-metadata", deny=())
    assert email in publication._scan_bytes(b" " + signoff, location="git-metadata", deny=())
    assert email in publication._scan_bytes(
        b"Signed-off-by: other[bot] <" + support + b">", location="git-metadata", deny=()
    )
    assert email in publication._scan_bytes(private_bot, location="git-metadata", deny=())
    assert denied in publication._scan_bytes(dependabot, location="git-metadata", deny=(b"dependabot",))
    assert denied in publication._scan_bytes(web_flow, location="git-metadata", deny=(b"github",))
    assert denied in publication._scan_bytes(signoff, location="git-metadata", deny=(b"support",))


def test_dependabot_commit_identity_is_public_and_denied_mail_still_blocks(tmp_path, monkeypatch):
    import os
    import hashlib
    monkeypatch.setattr(publication, "MAINTAINER_EMAIL_DIGESTS", frozenset({
        hashlib.sha256(b"ada@" + b"personal.example").hexdigest(),
    }))

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    bot = {
        **os.environ,
        "GIT_AUTHOR_NAME": "dependabot[bot]",
        "GIT_AUTHOR_EMAIL": "49699333+dependabot[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "GitHub",
        "GIT_COMMITTER_EMAIL": "noreply@" + "github.com",
    }
    message = "Bump synthetic dependency\n\nSigned-off-by: dependabot[bot] <support@" + "github.com>"
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", message],
        cwd=tmp_path,
        env=bot,
        check=True,
    )
    assert publication.scan_history(tmp_path, ()) == []
    assert any(
        item.rule == "operator-deny-term"
        for item in publication.scan_history(tmp_path, (b"dependabot",))
    )
    personal = dict(bot)
    personal["GIT_AUTHOR_EMAIL"] = "ada@" + "personal.example"
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "synthetic personal"],
        cwd=tmp_path,
        env=personal,
        check=True,
    )
    assert publication.Finding(
        "git-metadata", "maintainer-email-address"
    ) in publication.scan_history(tmp_path, ())


def test_reviewed_squash_coauthor_is_exact_commit_identity_and_trailer(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Synthetic Maintainer")
    git("config", "user.email", "maintainer@example.com")
    name = b"synthetic-public-contributor"
    email = b"12345+synthetic-public-contributor@users.noreply.github.com"
    trailer = b"Co-authored-by: " + name + b" <" + email + b">"
    git("commit", "--allow-empty", "-qm", "Synthetic squash\n\n" + trailer.decode())
    head = git("rev-parse", "HEAD")
    def findings(*commits):
        return publication.scan_history(tmp_path, (name,), public_git_identities=((name, email),),
                                        public_merge_commits=commits)
    assert findings()
    assert not findings(head)
    assert publication.scan_history(tmp_path, (name,), public_git_identities=((name, b"other@example.com"),),
                                    public_merge_commits=(head,))
    git("commit", "--allow-empty", "-qm", "Synthetic unreviewed squash\n\n" + trailer.decode())
    assert findings(head)
    second = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", name.decode() + " is outside the trailer\n\n" + trailer.decode())
    assert findings(head, second, git("rev-parse", "HEAD"))


def test_reviewed_email_disposition_is_exact_and_metadata_only(tmp_path, monkeypatch):
    import hashlib
    name = 'Synthetic Maintainer'
    email = 'contributor@' + 'synthetic.invalid'
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()
    git('init', '-q')
    git('config', 'user.name', name)
    git('config', 'user.email', email)
    git('commit', '--allow-empty', '-qm', 'synthetic approved attribution')
    commit = git('rev-parse', 'HEAD')
    digest = hashlib.sha256((name + '\0' + email).encode()).hexdigest()
    monkeypatch.setattr(publication, 'MAINTAINER_EMAIL_DIGESTS', frozenset({
        hashlib.sha256(email.encode().lower()).hexdigest(),
    }))
    expected = publication.Finding('git-metadata', 'maintainer-email-address')
    assert expected in publication.scan_history(tmp_path, ())
    monkeypatch.setattr(publication, 'REVIEWED_COMMIT_EMAIL_IDENTITIES', {commit: {digest}})
    assert publication.scan_history(tmp_path, ()) == []
    assert publication.Finding('git-metadata', 'operator-deny-term') in publication.scan_history(
        tmp_path, (name.encode(),))
    exception = publication._baseline_public_git_identities([(commit, name, email)])
    assert publication.scan_history(tmp_path, (name.encode(), email.encode()),
                                    baseline_public_git_identities=exception) == []
    import pytest
    with pytest.raises(RuntimeError, match='no-reply'):
        publication._baseline_public_git_identities([('0' * 40, name, email)])
    assert publication.Finding('fixture', 'non-example-email-address') in publication._scan_bytes(
        email.encode(), location='fixture', deny=())
    monkeypatch.setattr(publication, 'REVIEWED_COMMIT_EMAIL_IDENTITIES', {commit: {'0' * 64}})
    assert expected in publication.scan_history(tmp_path, ())
    monkeypatch.setattr(publication, 'REVIEWED_COMMIT_EMAIL_IDENTITIES', {commit: {digest}})
    git('commit', '--allow-empty', '-qm', 'synthetic later attribution')
    assert expected in publication.scan_history(tmp_path, ())


def test_reviewed_personal_baseline_does_not_cover_ancestors(tmp_path, monkeypatch):
    import hashlib
    name = 'Synthetic Contributor'
    email = 'maintainer@' + 'synthetic.invalid'
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()
    git('init', '-q')
    git('config', 'user.name', name)
    git('config', 'user.email', email)
    git('commit', '--allow-empty', '-qm', 'unreviewed ancestor')
    ancestor = git('rev-parse', 'HEAD')
    git('commit', '--allow-empty', '-qm', 'reviewed descendant')
    reviewed = git('rev-parse', 'HEAD')
    digest = hashlib.sha256((name + '\0' + email).encode()).hexdigest()
    # Both email fields have their generic disposition; only the descendant has
    # the separate, explicit operator-deny adjudication.
    monkeypatch.setattr(publication, 'REVIEWED_COMMIT_EMAIL_IDENTITIES',
                        {ancestor: {digest}, reviewed: {digest}})
    exception = publication._baseline_public_git_identities([(reviewed, name, email)])
    assert publication.Finding('git-metadata', 'operator-deny-term') in publication.scan_history(
        tmp_path, (name.encode(),), baseline_public_git_identities=exception)


@pytest.mark.parametrize('commit, digest, neighboring_commit', [
    ('6735691a938e42173c6c7a309b700be2e19c0668',
     '33a1b1226f681d54071f794ee5d2b2cb226c4d91efbe954997eab68adcd3f35b',
     '6735691a938e42173c6c7a309b700be2e19c0660'),
    ('a8f4fbe36439a374746f9c2b2ca9660d89793044',
     '33a1b1226f681d54071f794ee5d2b2cb226c4d91efbe954997eab68adcd3f35b',
     'a8f4fbe36439a374746f9c2b2ca9660d89793045'),
    ('624fbe4dcfbae093c442955bfb6cc4b6be6fff3e',
     '33a1b1226f681d54071f794ee5d2b2cb226c4d91efbe954997eab68adcd3f35b',
     '624fbe4dcfbae093c442955bfb6cc4b6be6fff3f'),
])
def test_reviewed_merge_attribution_has_one_exact_identity_digest(commit, digest, neighboring_commit):
    assert publication.REVIEWED_COMMIT_EMAIL_IDENTITIES[commit] == {digest}
    assert neighboring_commit not in publication.REVIEWED_COMMIT_EMAIL_IDENTITIES


@pytest.mark.parametrize('unreviewed_field', ['author-name', 'author-email', 'committer', 'message'])
def test_reviewed_attribution_does_not_cover_other_metadata(tmp_path, monkeypatch, unreviewed_field):
    import hashlib
    import os
    name = 'Synthetic Reviewer'
    email = 'reviewer@' + 'synthetic.invalid'
    other_email = 'another@' + 'synthetic.invalid'
    environment = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                       GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    message = 'Synthetic reviewed metadata boundary'
    if unreviewed_field == 'author-name':
        environment['GIT_AUTHOR_NAME'] = 'Different Synthetic Reviewer'
    elif unreviewed_field == 'author-email':
        environment['GIT_AUTHOR_EMAIL'] = other_email
    elif unreviewed_field == 'committer':
        environment['GIT_COMMITTER_EMAIL'] = other_email
    else:
        message += '\n\n' + email
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'commit', '--allow-empty', '-qm', message],
                   cwd=tmp_path, env=environment, check=True)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=tmp_path, text=True).strip()
    digest = hashlib.sha256((name + '\0' + email).encode()).hexdigest()
    monkeypatch.setattr(publication, 'MAINTAINER_EMAIL_DIGESTS', frozenset(
        hashlib.sha256(value.encode().lower()).hexdigest() for value in (email, other_email)
    ))
    monkeypatch.setattr(publication, 'REVIEWED_COMMIT_EMAIL_IDENTITIES', {commit: {digest}})
    assert publication.Finding('git-metadata', 'maintainer-email-address') in publication.scan_history(tmp_path, ())


@pytest.mark.parametrize("location,tail", [
    ("services/transcription/requirements-nemotron.lock", (b"9", b"90")),
    ("services/transcription/requirements-nemotron.lock", (b"7", b"77")),
    ("services/transcription/requirements-worker.lock", (b"9", b"90")),
    ("services/transcription/requirements-worker.lock", (b"7", b"77")),
    ("requirements/retrieval.lock", (b"7", b"77")),
])
def test_cuda_dependency_version_disposition_is_exact_and_context_bound(location, tail):
    version = b".".join((b"10", b"3", *tail))
    requirement = b"nvidia-curand-cu12==" + version + b" \\\n"
    finding = publication.Finding(location, "private-network-address")
    assert publication._scan_bytes(requirement, location=location, deny=()) == []
    assert publication._scan_bytes(requirement, location="git:" + "a" * 12 + ":" + location, deny=()) == []
    assert finding in publication._scan_bytes(requirement + b"# endpoint " + version, location=location, deny=())
    assert finding in publication._scan_bytes(requirement.replace(b"curand", b"other"), location=location, deny=())
    assert publication._scan_bytes(requirement, location="unreviewed.lock", deny=())
    assert publication.Finding(location, "operator-deny-term") in publication._scan_bytes(
        requirement, location=location, deny=(b"curand",))


def test_cuda_version_from_another_runtime_is_not_adjudicated():
    data = b"nvidia-curand-cu12==" + b".".join((b"10", b"3", b"9", b"90")) + b"\n"
    assert publication._scan_bytes(data, location="requirements/retrieval.lock", deny=())


def _contributor_repo(tmp_path, name, email, message, committer=None):
    import os
    def git(*args, env=None):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True, env=env).strip()
    git('init', '-q')
    env = {
        **os.environ,
        'GIT_AUTHOR_NAME': name,
        'GIT_AUTHOR_EMAIL': email,
        'GIT_COMMITTER_NAME': committer[0] if committer else 'GitHub',
        'GIT_COMMITTER_EMAIL': committer[1] if committer else 'noreply@' + 'github.com',
    }
    git('commit', '--allow-empty', '-qm', message, env=env)
    return git


def test_all_six_reviewed_commit_identities_are_unchanged():
    commits = {
        '6735691a938e42173c6c7a309b700be2e19c0668',
        '624fbe4dcfbae093c442955bfb6cc4b6be6fff3e',
        'd2f1e779e70063b94fece1291169f547c7c44d83',
        '4b8d74be09f41a443abb705bed1c6ab8bb5371c7',
        'f8ccae72b39704942c23caecb8195874a74e7699',
        'a8f4fbe36439a374746f9c2b2ca9660d89793044',
    }
    digest = '33a1b1226f681d54071f794ee5d2b2cb226c4d91efbe954997eab68adcd3f35b'
    assert publication.REVIEWED_COMMIT_EMAIL_IDENTITIES == {commit: {digest} for commit in commits}


def test_maintainer_denylist_has_three_address_digests():
    assert len(publication.MAINTAINER_EMAIL_DIGESTS) == 3
    assert all(re.fullmatch(r'[0-9a-f]{64}', value)
               for value in publication.MAINTAINER_EMAIL_DIGESTS)


@pytest.mark.parametrize('role', ['author', 'committer'])
@pytest.mark.parametrize('case', ['lower', 'upper'])
def test_maintainer_addresses_block_each_identity_field(tmp_path, monkeypatch, role, case):
    import hashlib
    denied = 'maintainer@' + 'synthetic.invalid'
    monkeypatch.setattr(publication, 'MAINTAINER_EMAIL_DIGESTS', frozenset({
        hashlib.sha256(denied.encode()).hexdigest(),
    }))
    denied = getattr(denied, case)()
    contributor = 'contributor@' + 'synthetic.invalid'
    _contributor_repo(tmp_path, 'Arbitrary Name', denied if role == 'author' else contributor,
                      'synthetic squash (#345)',
                      committer=('Different Name', denied if role == 'committer' else contributor))
    findings = publication.scan_history(tmp_path, ())
    assert findings == [publication.Finding('git-metadata', 'maintainer-email-address')]
    assert denied not in repr(findings)


@pytest.mark.parametrize('payload', ['file', 'name', 'message', 'trailer'])
def test_maintainer_hash_check_cannot_be_masked_by_other_email_exceptions(tmp_path, monkeypatch, payload):
    import hashlib
    email = 'maintainer@example.com'
    monkeypatch.setattr(publication, 'MAINTAINER_EMAIL_DIGESTS', frozenset({
        hashlib.sha256(email.encode()).hexdigest(),
    }))
    name = 'Synthetic Contributor'
    value = 'allowed@' + 'synthetic.invalid' + '\n' + email.upper()
    message = 'synthetic'
    if payload == 'name':
        name = email.upper()
    elif payload == 'message':
        message = value
    elif payload == 'trailer':
        message += '\n\nCo-authored-by: ' + name + ' <' + email + '>'
    _contributor_repo(tmp_path, name, 'contributor@example.com', message)
    if payload == 'file':
        # fixture-write: synthetic publication scanner input in the temporary repository.
        (tmp_path / 'fixture.txt').write_text(value)
        findings = publication.scan_tree(tmp_path, ())
    else:
        findings = publication.scan_history(tmp_path, ())
    assert any(item.rule == 'maintainer-email-address' for item in findings)
    assert email not in repr(findings).lower()


def test_unlisted_contributor_squash_author_and_committer_pass(tmp_path):
    git = _contributor_repo(tmp_path, 'New Contributor', 'author@' + 'synthetic.invalid',
                            'synthetic change (#345)',
                            committer=('Other Contributor', 'committer@' + 'synthetic.invalid'))
    assert publication.scan_history(tmp_path, ()) == []
    git('-c', 'user.name=Another Contributor', '-c', 'user.email=next@' + 'synthetic.invalid',
        'commit', '--allow-empty', '-qm', 'another squash (#346)')
    assert publication.scan_history(tmp_path, ()) == []


def test_denied_address_in_path_is_not_echoed(tmp_path, monkeypatch):
    import hashlib
    email = 'maintainer@example.com'
    monkeypatch.setattr(publication, 'MAINTAINER_EMAIL_DIGESTS', frozenset({
        hashlib.sha256(email.encode()).hexdigest(),
    }))
    directory = tmp_path / email.upper()
    directory.mkdir()
    # fixture-write: synthetic publication scanner input in the temporary repository.
    (directory / 'fixture.txt').write_text('synthetic')
    findings = publication.scan_tree(tmp_path, ())
    assert findings == [publication.Finding('tree-entry[1]-name', 'maintainer-email-address')]
    assert email not in repr(findings).lower()


@pytest.mark.parametrize('value,rule', [
    ('10.' + '23.45.67', 'private-network-address'),
    ('/home/' + 'specific.operator/runtime', 'personal-home-path'),
    ('-----BEGIN ' + 'PRIVATE KEY-----', 'private-key-material'),
    ('private-organization-marker', 'operator-deny-term'),
    ('name@' + 'synthetic.invalid', 'non-example-email-address'),
])
def test_contributor_identity_permission_preserves_other_checks(tmp_path, value, rule):
    _contributor_repo(tmp_path, value, 'contributor@' + 'synthetic.invalid', 'synthetic')
    assert publication.Finding('git-metadata', rule) in publication.scan_history(
        tmp_path, (b'private-organization-marker',))


def test_general_trailer_policy_preserves_multiple_attributions_and_deny_terms(tmp_path):
    name = 'Synthetic Contributor'
    email = 'contributor@' + 'synthetic.invalid'
    trailer = 'Co-authored-by: ' + name + ' <' + email + '>'
    git = _contributor_repo(tmp_path, name, email, 'Synthetic squash\n\n' + trailer)
    assert publication.scan_history(tmp_path, ()) == []
    git('-c', 'user.name=' + name, '-c', 'user.email=' + email,
        'commit', '--allow-empty', '-qm', 'Second\n\n' + trailer + '\r\n'
        + 'Signed-off-by: Another Person <other@' + 'synthetic.invalid>')
    assert publication.scan_history(tmp_path, ()) == []
    assert publication.Finding('git-metadata', 'operator-deny-term') in publication.scan_history(
        tmp_path, (b'Synthetic Contributor',))


SYNTHETIC_ADDRESSES = tuple(f'maintainer-{i}@' + 'synthetic.invalid' for i in range(3))
UNSUPPORTED_ATTRIBUTION = (
    'unsupported-attribution-address; use parse_attribution_address() '
    'with one RFC 5322 dot-atom address'
)


@pytest.fixture
def maintainer_addresses(monkeypatch):
    addresses = SYNTHETIC_ADDRESSES
    monkeypatch.setattr(publication, 'MAINTAINER_EMAIL_DIGESTS', frozenset(
        hashlib.sha256(address.encode()).hexdigest() for address in addresses
    ))
    return addresses


@pytest.mark.parametrize('role', ['author', 'committer', 'Co-Authored-By', 'Signed-off-by'])
@pytest.mark.parametrize('variant', ['plain', 'mixed-padding'])
@pytest.mark.parametrize('address_index', range(len(SYNTHETIC_ADDRESSES)))
def test_maintainer_addresses_block_identity_and_trailer_fields(
    tmp_path, maintainer_addresses, role, variant, address_index,
):
    address = maintainer_addresses[address_index]
    if variant == 'mixed-padding':
        address = '  ' + address.swapcase() + '  '
    ordinary = 'contributor@' + 'synthetic.invalid'
    message = 'Synthetic attribution'
    if role not in ('author', 'committer'):
        message += '\n\n' + role + ':   Synthetic Person   <' + address + '>'
    _contributor_repo(tmp_path, 'Synthetic Author', address if role == 'author' else ordinary,
                      message, committer=('Synthetic Committer', address if role == 'committer' else ordinary))
    assert publication.scan_history(tmp_path, ()) == [
        publication.Finding('git-metadata', 'maintainer-email-address')]


@pytest.mark.parametrize('trailer', [
    'Co-Authored-By: Claude Fable 5.2 <noreply@' + 'anthropic.com>',
    'Co-Authored-By: Synthetic Person <person@' + 'synthetic.invalid>',
    'Signed-off-by: Synthetic Person <person@' + 'synthetic.invalid>',
    'rEvIeWeD-bY :   Synthetic Person   <  person@' + 'synthetic.invalid  >',
    'Custom-token: Synthetic Person <person@' + 'synthetic.invalid>',
    "Signed-off-by: Synthetic Person <o'connor@" + 'synthetic.invalid>',
])
def test_final_trailer_addresses_are_allowed(tmp_path, trailer):
    _contributor_repo(tmp_path, 'Synthetic Author', 'author@example.com', 'Synthetic\n\n' + trailer)
    assert publication.scan_history(tmp_path, ()) == []


@pytest.mark.parametrize('layout', [
    '{address}', 'Synthetic\n\nContact {address}',
    'Synthetic\n\nCo-Authored-By: Person <{address}>\n\nLater paragraph',
    'Synthetic\n\nCo-Authored-By: Person <{address}> extra',
    'Synthetic\n\nContact {address}\nCo-Authored-By: Person <other@example.com>',
    'Synthetic\n\nCo Authored By: Person <{address}>',
    'Synthetic\n\n Co-Authored-By: Person <{address}>',
    'Co-Authored-By: Person <{address}>',
    'Synthetic\n\nCo-Authored-By: {address} <other@example.com>',
])
@pytest.mark.parametrize('address_index', [None, *range(len(SYNTHETIC_ADDRESSES))])
def test_non_trailer_email_content_retains_all_email_rules(
    tmp_path, maintainer_addresses, layout, address_index,
):
    address = maintainer_addresses[address_index] if address_index is not None else 'person@' + 'synthetic.invalid'
    _contributor_repo(tmp_path, 'Synthetic Author', 'author@example.com', layout.format(address=address))
    expected = [publication.Finding('git-metadata', 'non-example-email-address')]
    if address_index is not None:
        expected.insert(0, publication.Finding('git-metadata', 'maintainer-email-address'))
    assert publication.scan_history(tmp_path, ()) == expected


@pytest.mark.parametrize('value,rule', [
    ('deny-marker', 'operator-deny-term'),
    ('/home/' + 'specific.operator/runtime', 'personal-home-path'),
    ('-----BEGIN ' + 'PRIVATE KEY-----', 'private-key-material'),
    ('10.' + '23.45.67', 'private-network-address'),
    ('api_key = ' + chr(34) + 'A' * 32 + chr(34), 'literal-secret-shaped-value'),
])
def test_trailer_address_exemption_preserves_other_rules(tmp_path, value, rule):
    message = 'Synthetic\n\nSigned-off-by: ' + value + ' <person@' + 'synthetic.invalid>'
    _contributor_repo(tmp_path, 'Synthetic Author', 'author@example.com', message)
    assert publication.scan_history(tmp_path, (b'deny-marker',)) == [publication.Finding('git-metadata', rule)]


@pytest.mark.parametrize('address,rule', [
    ('deny-marker@' + 'synthetic.invalid', 'operator-deny-term'),
    ('person@fixture.' + 'internal', 'internal-fqdn'),
])
def test_non_email_rules_inspect_original_trailer_address(tmp_path, address, rule):
    message = 'Synthetic\n\nSigned-off-by: Synthetic Person <' + address + '>'
    _contributor_repo(tmp_path, 'Synthetic Author', 'author@example.com', message)
    assert publication.scan_history(tmp_path, (b'deny-marker',)) == [publication.Finding('git-metadata', rule)]


@pytest.mark.parametrize('value', [
    'Person <mailto:{address}>', 'Person <{address};>',
    'Person <{address},>', 'Person <{address}/path>',
    'Person <<{address}>>', 'Person <extra {address}>',
])
def test_malformed_trailer_values_keep_content_email_rules(tmp_path, value):
    address = 'person@' + 'synthetic.invalid'
    message = 'Synthetic\n\nSigned-off-by: ' + value.format(address=address)
    _contributor_repo(tmp_path, 'Synthetic Author', 'author@example.com', message)
    assert publication.scan_history(tmp_path, ()) == [
        publication.Finding('git-metadata', UNSUPPORTED_ATTRIBUTION),
        publication.Finding('git-metadata', 'non-example-email-address')]


@pytest.mark.parametrize('address_index', range(len(SYNTHETIC_ADDRESSES)))
def test_maintainer_file_content_retains_both_email_rules(tmp_path, maintainer_addresses, address_index):
    # fixture-write: synthetic address content for the working-tree scanner.
    (tmp_path / 'fixture.txt').write_text(maintainer_addresses[address_index])
    assert publication.scan_tree(tmp_path, ()) == [
        publication.Finding('fixture.txt', 'maintainer-email-address'),
        publication.Finding('fixture.txt', 'non-example-email-address'),
    ]


@pytest.mark.parametrize('address_index', [None, *range(len(SYNTHETIC_ADDRESSES))])
def test_crlf_trailers_preserve_raw_commit_bytes(tmp_path, maintainer_addresses, address_index):
    git = _contributor_repo(tmp_path, 'Synthetic Author', 'author@example.com', 'Synthetic')
    address = maintainer_addresses[address_index] if address_index is not None else "o'connor@" + 'synthetic.invalid'
    message = ('Synthetic\r\n\r\nSigned-off-by: Synthetic Person <' + address + '>\r\n').encode()
    original = subprocess.check_output(['git', 'cat-file', 'commit', 'HEAD'], cwd=tmp_path)
    raw = original.split(b'\n\n', 1)[0] + b'\n\n' + message
    # fixture-write: raw synthetic commit object with deliberately preserved CRLF bytes.
    oid = subprocess.check_output(['git', 'hash-object', '-t', 'commit', '-w', '--stdin'],
                                  cwd=tmp_path, input=raw).decode().strip()
    git('update-ref', 'HEAD', oid)
    assert subprocess.check_output(['git', 'cat-file', 'commit', 'HEAD'], cwd=tmp_path) == raw
    expected = [publication.Finding('git-metadata', 'maintainer-email-address')] if address_index is not None else []
    assert publication.scan_history(tmp_path, ()) == expected


def test_nul_inside_trailer_address_is_not_exempted():
    trailer = b'Signed-off-by: Synthetic Person <person@' + b'synthetic.invalid\0>'
    spans = publication._trailer_address_spans(trailer, trailer)
    assert spans == ((trailer.index(b'<') + 1, len(trailer) - 1),)
    assert [item.rule for item in publication._scan_bytes(
        trailer, location='git-metadata', deny=(), attribution_email_spans=spans,
    )] == [UNSUPPORTED_ATTRIBUTION, 'non-example-email-address']


@pytest.mark.parametrize('role', ['author', 'committer'])
@pytest.mark.parametrize('address', ['noreply@' + 'anthropic.com', "o'connor@" + 'synthetic.invalid'])
def test_other_address_syntax_is_allowed_in_identity_fields(tmp_path, role, address):
    _contributor_repo(tmp_path, 'Synthetic Author', address if role == 'author' else 'author@example.com',
                      'Synthetic', committer=('Synthetic Committer',
                                              address if role == 'committer' else 'commit@example.com'))
    assert publication.scan_history(tmp_path, ()) == []


@pytest.mark.parametrize('role', ['author', 'committer', 'trailer'])
def test_complete_attribution_address_is_hashed(tmp_path, monkeypatch, role):
    address = "o'connor@" + 'synthetic.invalid'
    monkeypatch.setattr(publication, 'MAINTAINER_EMAIL_DIGESTS', frozenset({
        hashlib.sha256(address.encode()).hexdigest(),
    }))
    message = 'Synthetic'
    if role == 'trailer':
        message += '\n\nSigned-off-by: Synthetic Person <' + address.upper() + '>'
    _contributor_repo(tmp_path, 'Synthetic Author', address if role == 'author' else 'author@example.com',
                      message, committer=('Synthetic Committer',
                                          address if role == 'committer' else 'commit@example.com'))
    assert publication.scan_history(tmp_path, ()) == [
        publication.Finding('git-metadata', 'maintainer-email-address')]


@pytest.mark.parametrize('role', ['author', 'committer', 'trailer'])
@pytest.mark.parametrize('address_index', range(len(SYNTHETIC_ADDRESSES)))
@pytest.mark.parametrize('prefix', ["other'", 'other=', 'other!'])
def test_distinct_attribution_containing_protected_token(tmp_path, maintainer_addresses, role, address_index, prefix):
    _attribution_repo(tmp_path, prefix + maintainer_addresses[address_index], role)
    assert publication.scan_history(tmp_path, ()) == []


def _attribution_repo(root, address, role):
    ordinary = 'contributor@' + 'synthetic.invalid'
    message = 'Synthetic'
    if role == 'trailer':
        message += '\n\nSigned-off-by: Synthetic Person <' + address + '>'
    return _contributor_repo(root, 'Synthetic Author', address if role == 'author' else ordinary,
                             message, committer=('Synthetic Committer', address if role == 'committer' else ordinary))


@pytest.mark.parametrize('role', ['author', 'committer', 'trailer'])
@pytest.mark.parametrize('value,content', [
    ('{p},', ['maintainer-email-address', 'non-example-email-address']),
    ("other..'{p}", ['maintainer-email-address', 'non-example-email-address']),
    ('.{p}', ['non-example-email-address']), ('{p_local}.@{d}', ['non-example-email-address']),
    ('person..x@{d}', ['non-example-email-address']),
    ('person@{d}/path', ['non-example-email-address']), ('person@{d};', ['non-example-email-address']),
    ('<person@{d}>', ['non-example-email-address']), ('mailto:person@{d}', ['non-example-email-address']),
    ('extra person@{d}', ['non-example-email-address']),
    ('@{d}', []), ('person@synthetic', []), ('person@é.invalid', []), ('', []),
    ('person@{d}é', ['non-example-email-address']),
    ('automation[bot]@{d},', ['non-example-email-address']), ('automation[bot]@synthetic', []),
])
def test_malformed_attribution_exact_bytes(tmp_path, maintainer_addresses, role, value, content):
    address = value.format(p=maintainer_addresses[0], p_local=maintainer_addresses[0].split('@')[0],
                           d='synthetic.invalid').encode()
    if role == 'trailer':
        data = b'Signed-off-by: Synthetic Person <' + address + b'>'
        spans = publication._trailer_address_spans(data, data)
    else:
        data = b'Synthetic Person\0' + address
        spans = ((len(b'Synthetic Person\0'), len(data)),)
    assert len(spans) == 1
    assert [item.rule for item in publication._scan_bytes(
        data, location='git-metadata', deny=(), attribution_email_spans=spans,
    )] == [UNSUPPORTED_ATTRIBUTION, *content]
    if role == 'trailer' or b'<' not in address:
        git = _attribution_repo(tmp_path, address.decode() if role == 'trailer' else 'person@' + 'synthetic.invalid', role)
        if role != 'trailer':
            raw = subprocess.check_output(['git', 'cat-file', 'commit', 'HEAD'], cwd=tmp_path)
            raw = re.sub(rb'(?m)^' + role.encode() + rb' [^\n]*',
                         role.encode() + b' Synthetic Person <' + address + b'> 1234567890 +0000', raw)
            # fixture-write: raw synthetic identity bytes bypass Git commit input cleanup.
            oid = subprocess.check_output(['git', 'hash-object', '-t', 'commit', '-w', '--stdin'], cwd=tmp_path, input=raw)
            git('update-ref', 'HEAD', oid.decode().strip())
        assert [item.rule for item in publication.scan_history(tmp_path, ())] == [UNSUPPORTED_ATTRIBUTION, *content]



@pytest.mark.parametrize('role', ['author', 'committer', 'trailer'])
@pytest.mark.parametrize('address', [
    *[local + '@' + 'synthetic.invalid' for local in ['person!x', 'person+tag', "o'connor", 'a.b', 'automation[bot]']],
    *[address.decode() for address in sorted(publication.PUBLIC_BOT_NOREPLY_ADDRESSES)],
])
def test_attribution_grammar_controls(tmp_path, role, address):
    _attribution_repo(tmp_path, address, role)
    assert publication.scan_history(tmp_path, ()) == []


@pytest.mark.parametrize('address', sorted(publication.PUBLIC_BOT_NOREPLY_ADDRESSES))
def test_automation_content_and_malformed_trailer(address):
    assert publication._scan_bytes(address, location='fixture.txt', deny=()) == []
    trailer = b'Signed-off-by: Synthetic Person <' + address + b',>'
    assert [item.rule for item in publication._scan_bytes(
        trailer, location='git-metadata', deny=(),
        attribution_email_spans=publication._trailer_address_spans(trailer, trailer),
    )] == [UNSUPPORTED_ATTRIBUTION]


def test_nameless_trailer_is_content(tmp_path):
    _contributor_repo(tmp_path, 'Synthetic Author', 'author@example.com',
                      'Synthetic\n\nSigned-off-by:   <person@' + 'synthetic.invalid>')
    assert publication.scan_history(tmp_path, ()) == [publication.Finding('git-metadata', 'non-example-email-address')]


def test_reviewed_identity_does_not_grant_trailer(tmp_path, monkeypatch, maintainer_addresses):
    address = maintainer_addresses[0]
    git = _contributor_repo(tmp_path, 'Synthetic Author', address,
                            'Synthetic\n\nSigned-off-by: Synthetic Person <' + address + '>')
    monkeypatch.setattr(publication, 'REVIEWED_COMMIT_EMAIL_IDENTITIES', {
        git('rev-parse', 'HEAD'): {publication._identity_digest(b'Synthetic Author', address.encode())},
    })
    assert publication.scan_history(tmp_path, ()) == [publication.Finding('git-metadata', 'maintainer-email-address')]


@pytest.mark.parametrize('local,domain,valid', [
    (b"Az09!#$%&'*+/=?^_`{|}~[]-", b'synthetic.invalid', True),
    (b'a' * 64, b'synthetic.123', True),
    (b'a', b'.'.join([b'x' * 63] * 3 + [b'x' * 61]), True),
    (b'a' * 65, b'synthetic.invalid', False),
    (b'a', b'.'.join([b'x' * 63] * 4), False), (b'a', b'x' * 64 + b'.invalid', False),
    (b'a', b'-synthetic.invalid', False), (b'a', b'synthetic-.invalid', False),
    (b'a', b'synthetic..invalid', False), (b'a', b'', False),
    *[(local, b'synthetic.invalid', False) for local in [b' a', b'a ', b'"a"', b'a\0', b'a@b', b'a,b', b'a;b', b'a:b', b'a(b)', b'a<b>', b'a\xff']],
])
def test_parse_attribution_address_exact_grammar(local, domain, valid):
    value = local + b'@' + domain
    assert publication.parse_attribution_address(value) == (value if valid else None)
