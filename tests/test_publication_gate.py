from __future__ import annotations

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
    (tmp_path / "safe.txt").write_text("synthetic fixture", encoding="utf-8")
    (tmp_path / "case.sqlite3").write_bytes(b"not-a-real-database")
    (tmp_path / "link").symlink_to(tmp_path / "safe.txt")
    findings = {(item.location, item.rule) for item in publication.scan_tree(tmp_path, ())}
    assert ("case.sqlite3", "runtime-or-credential-file") in findings
    assert ("link", "symbolic-link") in findings


def test_publication_gate_rejects_extended_credential_suffixes(tmp_path) -> None:
    for suffix in (".cer", ".crt", ".der", ".jks", ".keystore"):
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
    private_context.write_text(public_name, encoding="utf-8")
    assert publication.Finding(
        "operator-notes.txt", "operator-deny-term"
    ) in publication.scan_tree(
        tmp_path, deny, public_clone_urls=(clone_url.encode(),)
    )
    private_context.unlink()
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
    with pdf.open("wb") as handle:
        writer.write(handle)

    archive = tmp_path / "synthetic.docx"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("word/document.xml", "server " + "10." + "42.19.7")

    findings = {(item.location, item.rule) for item in publication.scan_tree(tmp_path, ())}
    assert ("synthetic.pdf#pdf-content", "non-example-email-address") in findings
    assert ("synthetic.docx!member[1]", "private-network-address") in findings


def test_archive_member_names_are_scanned_without_echoing_private_values() -> None:
    payload = __import__("io").BytesIO()
    private_name = "records/" + "10." + "91.8.7" + "/notes.txt"
    with zipfile.ZipFile(payload, "w") as output:
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
    residue.write_text("service=" + "10." + "77.8.9", encoding="utf-8")
    subprocess.run(["git", "add", "old-config.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "synthetic first"], cwd=tmp_path, check=True)
    residue.unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove fixture"], cwd=tmp_path, check=True)

    findings = publication.scan_history(tmp_path, ())
    assert any(item.rule == "private-network-address" for item in findings)
    assert any("old-config.txt" in item.location for item in findings)


def test_history_scan_checks_author_and_committer_identity(tmp_path) -> None:
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
    (tmp_path / "README.md").write_text("synthetic\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "synthetic"], cwd=tmp_path, check=True)

    assert publication.Finding(
        "git-metadata", "non-example-email-address"
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


def test_dependabot_commit_identity_is_public_and_personal_mail_still_blocks(tmp_path):
    import os

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
        "git-metadata", "non-example-email-address"
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
    expected = publication.Finding('git-metadata', 'non-example-email-address')
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
    monkeypatch.setattr(publication, 'REVIEWED_COMMIT_EMAIL_IDENTITIES', {commit: {digest}})
    assert publication.Finding('git-metadata', 'non-example-email-address') in publication.scan_history(tmp_path, ())


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


def test_seeded_contributor_identities_are_digests_only():
    assert publication.REVIEWED_CONTRIBUTOR_EMAIL_IDENTITIES
    for digest in publication.REVIEWED_CONTRIBUTOR_EMAIL_IDENTITIES:
        assert re.fullmatch(r'[0-9a-f]{64}', digest)


def test_reviewed_contributor_squash_commit_passes_on_any_commit(tmp_path, monkeypatch):
    import hashlib
    name = 'Synthetic Contributor'
    email = 'contributor@' + 'synthetic.invalid'
    digest = hashlib.sha256((name + '\0' + email).encode()).hexdigest()
    trailer = 'Co-authored-by: ' + name + ' <' + email + '>'
    message = 'docs: synthetic squash (#1)\n\n* synthetic change\n\n' + trailer
    git = _contributor_repo(tmp_path, name, email, message)
    email_rule = publication.Finding('git-metadata', 'non-example-email-address')
    assert email_rule in publication.scan_history(tmp_path, ())
    monkeypatch.setattr(publication, 'REVIEWED_CONTRIBUTOR_EMAIL_IDENTITIES', frozenset({digest}))
    assert publication.scan_history(tmp_path, ()) == []
    # Any later commit with the same identity also passes (squash SHAs are new).
    git('-c', 'user.name=' + name, '-c', 'user.email=' + email,
        'commit', '--allow-empty', '-qm', 'second (#2)\n\n' + trailer + '\r')
    assert publication.scan_history(tmp_path, ()) == []
    # Deny terms still apply to the listed identity and message.
    assert publication.Finding('git-metadata', 'operator-deny-term') in publication.scan_history(
        tmp_path, (b'Synthetic Contributor',))


def test_reviewed_contributor_identity_is_exact_and_email_rule_only(tmp_path, monkeypatch):
    import hashlib
    name = 'Synthetic Contributor'
    email = 'contributor@' + 'synthetic.invalid'
    digest = hashlib.sha256((name + '\0' + email).encode()).hexdigest()
    monkeypatch.setattr(publication, 'REVIEWED_CONTRIBUTOR_EMAIL_IDENTITIES', frozenset({digest}))
    email_rule = publication.Finding('git-metadata', 'non-example-email-address')
    cases = [
        (name, 'Contributor@' + 'synthetic.invalid', 'case variant'),
        ('Spoofed Name', email, 'name spoof'),
        ('Synthetic  Contributor', email, 'internal whitespace'),
        (name, ' ' + email + 'x', 'email suffix'),
        ('Unlisted Person', 'person@' + 'synthetic.invalid', 'unlisted'),
    ]
    for index, (author, address, label) in enumerate(cases):
        repo = tmp_path / str(index)
        repo.mkdir()
        _contributor_repo(repo, author, address, label)
        assert email_rule in publication.scan_history(repo, ()), label
    trailer = 'Co-authored-by: ' + name + ' <' + email + '>'
    extra = 'other@' + 'synthetic.invalid'
    bad_messages = [
        'x\n\n' + trailer + ' ' + extra,
        'x\n\n' + trailer + '\nCo-authored-by: Unlisted <' + extra + '>',
        'x\n\nCo-authored-by: Spoofed <' + email + '>',
        'x\n\n ' + trailer,
        'x\n\nsee ' + extra + '\n' + trailer,
    ]
    for index, message in enumerate(bad_messages):
        repo = tmp_path / f'm{index}'
        repo.mkdir()
        _contributor_repo(repo, name, email, message)
        assert email_rule in publication.scan_history(repo, ()), message
    secret = 'api_key = "' + 'A' * 32 + '"'
    repo = tmp_path / 'secret'
    repo.mkdir()
    _contributor_repo(repo, name, email, 'x\n\n' + secret + '\n' + trailer)
    rules = {item.rule for item in publication.scan_history(repo, ())}
    assert rules and 'non-example-email-address' not in rules
    repo = tmp_path / 'committer'
    repo.mkdir()
    _contributor_repo(repo, name, email, 'x', committer=('Maintainer', 'maint@' + 'synthetic.invalid'))
    assert email_rule in publication.scan_history(repo, ())
