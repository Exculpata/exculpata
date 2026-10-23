"""Exercise bounded APT setup without root access, network, or package changes."""
import importlib.util
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_browser_deps", ROOT / "scripts/install-ci-browser-deps.py")
deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deps)

PACKAGE_LIST = [
    "libnss3", "libatk-bridge2.0-0", "libxkbcommon0", "libxcomposite1",
    "libxdamage1", "libxrandr2", "libgbm1", "libasound2t64", "libgtk-3-0",
    "fonts-liberation", "ffmpeg",
]
MIRROR_LIST = (
    "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
    "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
    "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
)
SIGNED_SOURCES = (
    "Types: deb\nURIs: mirror+file:/etc/apt/apt-mirrors.txt\n"
    "Suites: noble noble-updates noble-backports noble-security\n"
    "Components: main restricted universe multiverse\n"
    "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
)


@pytest.fixture
def apt(tmp_path, monkeypatch):
    mirrors, sources = tmp_path / "apt-mirrors.txt", tmp_path / "ubuntu.sources"
    mirrors.write_text(MIRROR_LIST)
    sources.write_text(SIGNED_SOURCES)
    calls, acquisition_results, install_results = [], [], []

    def run(command, **kwargs):
        calls.append((command, kwargs, mirrors.read_text(), sources.read_bytes()))
        if command[:2] == ["sudo", "tee"]:
            assert command[2] == str(mirrors)
            mirrors.write_bytes(kwargs["input"])
            result = 0
        elif command[:2] == ["sudo", "timeout"]:
            result = acquisition_results.pop(0) if acquisition_results else 0
        elif command[:2] == ["sudo", "chown"]:
            result = 0
        elif command[:3] == ["sudo", "apt-get", "install"]:
            result = install_results.pop(0) if install_results else 0
        else:
            pytest.fail(f"Unexpected external command: {command}")
        if result and kwargs.get("check"):
            raise subprocess.CalledProcessError(result, command)
        return subprocess.CompletedProcess(command, result)

    monkeypatch.setattr(deps.subprocess, "run", run)
    return mirrors, sources, calls, acquisition_results, install_results


def commands(apt):
    return [command for command, *_ in apt[2]]


def assert_full_packages(command):
    assert command[-len(PACKAGE_LIST):] == PACKAGE_LIST
    assert "--no-install-recommends" in command


def test_primary_success_installs_only_after_bounded_acquisition(apt):
    deps.install_dependencies(*apt[:2])
    update, download, install = commands(apt)
    assert update[:7] == ["sudo", "timeout", "--signal=TERM", "--kill-after=10s", "60s", "apt-get", "-o"]
    assert update[-2:] == ["update", "--error-on=any"]
    assert download[:6] == ["sudo", "timeout", "--signal=TERM", "--kill-after=10s", "180s", "apt-get"]
    assert "--download-only" in download
    assert_full_packages(download)
    assert install[:3] == ["sudo", "apt-get", "install"]
    assert "--no-download" in install and "timeout" not in install
    assert_full_packages(install)
    assert not any("Dir::Cache::" in argument or "Keep-Downloaded" in argument
                   for command in commands(apt) for argument in command)
    assert apt[0].read_text() == MIRROR_LIST
    assert not any("Dir::Etc::" in argument for command in commands(apt) for argument in command)


@pytest.mark.parametrize("extra_location", ["sources.list", "third-party.list", "third-party.sources"])
@pytest.mark.parametrize("phase", ["update", "download", "install"])
def test_fallback_excludes_extra_sources_from_each_apt_phase(apt, monkeypatch, extra_location, phase):
    # Model APT's main source file plus *.list/*.sources source-parts selection.
    # A third-party outage triggers the primary retry; its package candidates
    # must also stay out of both fallback install invocations.
    apt_root = apt[1].parent
    parts = apt_root / "sources.list.d"
    parts.mkdir()
    ubuntu = parts / "ubuntu.sources"
    ubuntu.write_text(SIGNED_SOURCES)
    extra = apt_root / extra_location if extra_location == "sources.list" else parts / extra_location
    extra.write_text(
        SIGNED_SOURCES.replace("mirror+file:/etc/apt/apt-mirrors.txt", "https://third-party.example.test/ubuntu/")
        if extra.suffix == ".sources" else "deb https://third-party.example.test/ubuntu/ noble main\n"
    )
    original_extra = extra.read_bytes()
    original_run = deps.subprocess.run
    observed = []

    def run(command, **kwargs):
        if "apt-get" not in command:
            return original_run(command, **kwargs)
        options = dict(command[index + 1].split("=", 1)
                       for index, argument in enumerate(command) if argument == "-o")
        main = Path(options.get("Dir::Etc::sourcelist", str(apt_root / "sources.list")))
        source_parts = Path(options.get("Dir::Etc::sourceparts", str(parts)))
        selected = [main] if main.is_file() else []
        if source_parts.is_dir():
            selected.extend(path for path in source_parts.iterdir() if path.suffix in {".list", ".sources"})
        current_phase = "update" if "update" in command else "download" if "--download-only" in command else "install"
        fallback = apt[0].read_text() != MIRROR_LIST
        observed.append((fallback, current_phase))
        if not fallback:
            assert extra in selected  # Normal runner source selection remains intact.
            assert not any(key.startswith("Dir::Etc::") for key in options)
            return subprocess.CompletedProcess(command, 100)
        if current_phase == phase:
            assert selected == [ubuntu.resolve()]
            assert extra not in selected  # No third-party index or package candidate.
            assert ubuntu.read_text() == SIGNED_SOURCES
            if current_phase == "update":
                assert "--error-on=any" in command
            else:
                assert_full_packages(command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(deps.subprocess, "run", run)
    deps.install_dependencies(apt[0], ubuntu)
    assert observed == [(False, "update"), (True, "update"), (True, "download"), (True, "install")]
    assert extra.read_bytes() == original_extra
    assert apt[0].read_text() == MIRROR_LIST


@pytest.mark.parametrize("results", [[100], [124], [0, 100], [0, 124], [0, 137]])
def test_failed_or_timed_out_acquisition_gets_one_official_fallback(apt, results):
    apt[3].extend(results)
    deps.install_dependencies(*apt[:2])
    writes = [call for call in apt[2] if call[0][:2] == ["sudo", "tee"]]
    assert len(writes) == 2
    replacement = writes[0][1]["input"].decode()
    assert replacement == (
        "https://archive.ubuntu.com/ubuntu/\tpriority:1\n"
        "https://security.ubuntu.com/ubuntu/\tpriority:2\n"
    )
    install = next(call for call in apt[2] if call[0][:3] == ["sudo", "apt-get", "install"])
    assert install[2] == replacement
    assert_full_packages(install[0])
    assert apt[0].read_text() == MIRROR_LIST
    assert apt[1].read_text() == SIGNED_SOURCES
    assert all(call[3] == SIGNED_SOURCES.encode() for call in apt[2])
    for command in commands(apt):
        assert not any(flag in command for flag in ("--ignore-missing", "--allow-unauthenticated", "--allow-insecure-repositories"))


@pytest.mark.parametrize("results", [[100, 100], [0, 124, 100], [124, 0, 124], [0, 100, 0, 100]])
def test_both_attempts_failing_never_installs_and_restores_mirrors(apt, results):
    apt[3].extend(results)
    with pytest.raises(RuntimeError, match="both attempts"):
        deps.install_dependencies(*apt[:2])
    assert not any(command[:3] == ["sudo", "apt-get", "install"] for command in commands(apt))
    assert sum(command[:2] == ["sudo", "tee"] for command in commands(apt)) == 2
    assert apt[0].read_text() == MIRROR_LIST


@pytest.mark.parametrize("fallback", [False, True])
def test_installation_failure_is_not_retried_or_timed_out(apt, fallback):
    if fallback:
        apt[3].append(124)
    apt[4].append(100)
    with pytest.raises(subprocess.CalledProcessError):
        deps.install_dependencies(*apt[:2])
    installs = [command for command in commands(apt) if command[:3] == ["sudo", "apt-get", "install"]]
    assert len(installs) == 1 and "timeout" not in installs[0]
    assert apt[0].read_text() == MIRROR_LIST


@pytest.mark.parametrize("invalid", ["missing-official", "different-sources", "commented-source"])
def test_fallback_refuses_unrecognized_runner_configuration(apt, invalid):
    apt[3].append(124)
    if invalid == "missing-official":
        apt[0].write_text("http://azure.archive.ubuntu.com/ubuntu/\n")
    else:
        apt[1].write_text(SIGNED_SOURCES.replace("mirror+file:/etc/apt/apt-mirrors.txt", "https://archive.ubuntu.com/ubuntu/"))
        if invalid == "commented-source":
            with apt[1].open("a") as stream:
                stream.write("# URIs: mirror+file:/etc/apt/apt-mirrors.txt\n")
    with pytest.raises(ValueError):
        deps.install_dependencies(*apt[:2])
    assert len(commands(apt)) == 1


@pytest.mark.parametrize("layout", [
    SIGNED_SOURCES.replace("URIs: mirror+file:/etc/apt/apt-mirrors.txt",
                           "URIs: mirror+file:/etc/apt/apt-mirrors.txt https://unexpected.example.test/ubuntu/"),
    "Enabled: no\n" + SIGNED_SOURCES + "\n" + SIGNED_SOURCES.replace(
        "mirror+file:/etc/apt/apt-mirrors.txt", "https://unexpected.example.test/ubuntu/"),
], ids=["mixed-uris", "disabled-expected-active-unknown"])
def test_fallback_rejects_mixed_uris_and_disabled_expected_stanza(apt, layout):
    apt[3].append(124)
    apt[1].write_text(layout)
    with pytest.raises(ValueError):
        deps.install_dependencies(*apt[:2])
    assert len(commands(apt)) == 1  # No mirror write, second acquisition or install.
    assert apt[0].read_text() == MIRROR_LIST
    assert apt[1].read_text() == layout


@pytest.mark.parametrize("layout", [
    pytest.param(SIGNED_SOURCES.replace("URIs: mirror+file:/etc/apt/apt-mirrors.txt",
        "URIs: mirror+file:/etc/apt/apt-mirrors.txt\n https://unexpected.example.test/ubuntu/"), id="folded-extra-uri"),
    pytest.param(SIGNED_SOURCES + "\n" + SIGNED_SOURCES.replace(
        "mirror+file:/etc/apt/apt-mirrors.txt", "https://unexpected.example.test/ubuntu/"), id="extra-active-stanza"),
    pytest.param("Enabled: no\n" + SIGNED_SOURCES, id="all-disabled"),
    pytest.param(SIGNED_SOURCES.replace("URIs: mirror+file:/etc/apt/apt-mirrors.txt\n", ""), id="missing-uris"),
    pytest.param(SIGNED_SOURCES.replace("URIs: mirror+file:/etc/apt/apt-mirrors.txt", "URIs:"), id="empty-uris"),
    pytest.param(SIGNED_SOURCES + "uris: https://unexpected.example.test/ubuntu/\n", id="duplicate-uris"),
    pytest.param("Enabled: no\nenabled: yes\n" + SIGNED_SOURCES, id="duplicate-enabled"),
    pytest.param(" mirror+file:/etc/apt/apt-mirrors.txt\n" + SIGNED_SOURCES, id="orphan-continuation"),
    pytest.param(SIGNED_SOURCES + "Malformed line\n", id="missing-colon"),
    pytest.param(SIGNED_SOURCES + "Invalid Field: value\n", id="invalid-field-name"),
    pytest.param("Enabled:\n" + SIGNED_SOURCES, id="empty-enabled"),
    pytest.param("Enabled: perhaps\n" + SIGNED_SOURCES, id="unknown-enabled"),
    pytest.param(SIGNED_SOURCES.replace("Types: deb\n", ""), id="missing-types"),
    pytest.param(SIGNED_SOURCES.replace("Types: deb", "Types: invalid"), id="unknown-type"),
    pytest.param(SIGNED_SOURCES.replace("Suites: noble noble-updates noble-backports noble-security\n", ""), id="missing-suites"),
    pytest.param(SIGNED_SOURCES.replace("Components: main restricted universe multiverse\n", ""), id="missing-components"),
    pytest.param(SIGNED_SOURCES.replace("Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n", ""), id="missing-signed-by"),
    pytest.param("# URIs: mirror+file:/etc/apt/apt-mirrors.txt\n\n", id="comments-only"),
])
def test_fallback_rejects_ambiguous_or_malformed_sources_before_mutation(apt, layout):
    apt[3].append(124)
    apt[1].write_text(layout)
    with pytest.raises(ValueError):
        deps.install_dependencies(*apt[:2])
    assert len(commands(apt)) == 1
    assert apt[0].read_text() == MIRROR_LIST
    assert apt[1].read_text() == layout


def test_fallback_preserves_multiple_active_folded_stanzas_and_disabled_sources(apt):
    primary = SIGNED_SOURCES.replace("URIs: mirror+file:/etc/apt/apt-mirrors.txt",
                                    "URIs:\n mirror+file:/etc/apt/apt-mirrors.txt")
    primary = primary.replace("Types: deb", "Types: deb\n deb-src")
    security = SIGNED_SOURCES.replace("noble noble-updates noble-backports noble-security", "noble-security")
    disabled = SIGNED_SOURCES.replace("mirror+file:/etc/apt/apt-mirrors.txt", "https://unused.example.test/ubuntu/")
    layout = "# Synthetic Ubuntu source configuration\nEnabled: yes\n" + primary + "\n" + security + "\nEnabled: no\n" + disabled
    apt[1].write_text(layout)
    apt[3].append(124)
    deps.install_dependencies(*apt[:2])
    assert any(command[:3] == ["sudo", "apt-get", "install"] for command in commands(apt))
    assert apt[0].read_text() == MIRROR_LIST
    assert apt[1].read_text() == layout
    assert all(call[3] == layout.encode() for call in apt[2])


def test_partial_mirror_write_failure_restores_original_without_installing(apt, monkeypatch):
    apt[3].append(124)
    original_run = deps.subprocess.run
    failed = False

    def fail_write(command, **kwargs):
        nonlocal failed
        if command[:2] == ["sudo", "tee"] and not failed:
            failed = True
            apt[0].write_bytes(kwargs["input"][:15])
            raise subprocess.CalledProcessError(1, command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(deps.subprocess, "run", fail_write)
    with pytest.raises(subprocess.CalledProcessError):
        deps.install_dependencies(*apt[:2])
    assert apt[0].read_text() == MIRROR_LIST
    assert not any(command[:3] == ["sudo", "apt-get", "install"] for command in commands(apt))


@pytest.mark.parametrize("system,machine,github,release", [
    ("darwin", "arm64", "true", {"ID": "ubuntu", "VERSION_ID": "24.04"}),
    ("linux", "aarch64", "true", {"ID": "ubuntu", "VERSION_ID": "24.04"}),
    ("linux", "x86_64", "false", {"ID": "ubuntu", "VERSION_ID": "24.04"}),
    ("linux", "x86_64", "true", {"ID": "debian", "VERSION_ID": "12"}),
])
def test_cli_refuses_other_hosts_before_running_commands(apt, monkeypatch, system, machine, github, release):
    monkeypatch.setattr(deps.sys, "platform", system)
    monkeypatch.setattr(deps.platform, "machine", lambda: machine)
    monkeypatch.setattr(deps.platform, "freedesktop_os_release", lambda: release)
    monkeypatch.setenv("GITHUB_ACTIONS", github)
    with pytest.raises(RuntimeError):
        deps.main()
    assert not commands(apt)


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("failure", [None, "acquisition", "install"])
def test_custom_packages_and_archives_cover_every_phase(apt, fallback, failure):
    archives = apt[0].parent / "apt archives"
    packages = ("synthetic-package", "synthetic-media")
    apt[3].extend([0, 124] if fallback else [])
    if failure == "acquisition":
        apt[3].extend([0, 124] if fallback else [0, 124, 0, 124])
    elif failure == "install":
        apt[4].append(100)
    if failure:
        with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
            deps.install_dependencies(*apt[:2], packages=packages, archives=archives)
    else:
        deps.install_dependencies(*apt[:2], packages=packages, archives=archives)
    assert (archives / "partial").is_dir()
    phases = []
    for command in commands(apt):
        if "apt-get" not in command:
            continue
        assert command.count(f"Dir::Cache::archives={archives}") == 1
        assert command.count("APT::Keep-Downloaded-Packages=true") == 1
        phase = "update" if "update" in command else "download" if "--download-only" in command else "install"
        phases.append(phase)
        if phase != "update":
            assert command[-len(packages):] == list(packages)
            assert not set(PACKAGE_LIST) & set(command)
    assert phases[:2] == ["update", "download"]
    if fallback or failure == "acquisition":
        assert phases[2:4] == ["update", "download"]
    chowns = [command for command in commands(apt) if command[:2] == ["sudo", "chown"]]
    assert chowns == ([] if failure else [
        ["sudo", "chown", "-R", f"{deps.os.getuid()}:{deps.os.getgid()}", str(archives)],
    ])
    if not failure:
        assert phases[-1] == "install" and commands(apt)[-1] == chowns[0]


@pytest.mark.parametrize("custom", [False, True])
def test_cli_preserves_defaults_and_accepts_packages_and_archives(apt, monkeypatch, custom):
    monkeypatch.setattr(deps.sys, "platform", "linux")
    monkeypatch.setattr(deps.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(deps.platform, "freedesktop_os_release", lambda: {"ID": "ubuntu", "VERSION_ID": "24.04"})
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    archives = apt[0].parent / "apt archives"
    monkeypatch.setattr(deps.sys, "argv", ["install-ci-browser-deps.py"] + (
        ["--archives", str(archives), "synthetic-package"] if custom else []))
    assert deps.main() == 0
    update, download, install = commands(apt)[:3]
    if custom:
        assert f"Dir::Cache::archives={archives}" in update
        assert download[-1] == install[-1] == "synthetic-package"
        assert commands(apt)[-1][:2] == ["sudo", "chown"]
    else:
        assert_full_packages(download)
        assert_full_packages(install)
        assert len(commands(apt)) == 3
