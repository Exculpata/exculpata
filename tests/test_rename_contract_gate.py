"""New legacy identifiers require an explicit compatibility contract."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from string import Formatter

import pytest

from case_intelligence.legacy_names import LEGACY_STEM, NAME_CONTRACTS, NameContract

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rename_contract_check", ROOT / "scripts/rename_contract_check.py"
)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def test_repository_operational_names_are_registered():
    assert checker.findings(ROOT) == []


@pytest.mark.parametrize("relative", ["src/synthetic.py", "docs/SYNTHETIC.md", "docs/BRANDING.md"])
def test_unknown_legacy_identifier_in_code_or_docs_fails(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text('schema = "recordbench-unregistered-format-v99"\n')
    found = checker.findings(tmp_path)
    assert found == [checker.Finding(relative + ":1", "recordbench-unregistered-format-v99")]


def test_unknown_legacy_filename_fails_even_with_empty_contents(tmp_path):
    path = tmp_path / "recordbench-unknown-tool.py"
    path.touch()
    assert checker.findings(tmp_path) == [checker.Finding(path.name + ":1", "recordbench-unknown-tool.py")]


@pytest.mark.parametrize("filename,data", [
    ("recordbench-unknown-tool", b""),
    ("recordbench-unknown-tool.bin", b"\0synthetic"),
    ("recordbench-unknown-tool.py", b"\0synthetic"),
    ("recordbench-unknown-tool.sh", b"\xffsynthetic"),
])
def test_legacy_filenames_are_checked_before_content_filters(tmp_path, filename, data):
    path = tmp_path / "scripts" / filename
    path.parent.mkdir()
    path.write_bytes(data)
    expected = [checker.Finding(f"scripts/{filename}:1", filename)]
    if data.startswith(b"\xff"):
        expected.append(checker.Finding(f"scripts/{filename}:1", "unsupported undecodable operational source"))
    if filename.endswith('.py') and b'\x00' in data:
        expected.append(checker.Finding(f"scripts/{filename}:1", 'unsupported Python parse; use case_intelligence.legacy_names.name_value'))
    assert checker.findings(tmp_path) == expected


@pytest.mark.parametrize("child", [None, "config.py", "config", "config.bin"])
def test_legacy_directory_names_are_checked_independently_of_children(tmp_path, child):
    directory = tmp_path / "services" / "recordbench-unknown-service"
    directory.mkdir(parents=True)
    if child is not None:
        (directory / child).touch()
    assert checker.findings(tmp_path) == [
        checker.Finding("services/recordbench-unknown-service:1", "recordbench-unknown-service")
    ]


def test_registered_parent_does_not_hide_undeclared_path_components(tmp_path):
    directory = tmp_path / "recordbench-node" / "recordbench-unknown-service"
    directory.mkdir(parents=True)
    (directory / "recordbench-unknown-tool").touch()
    assert checker.findings(tmp_path) == [
        checker.Finding("recordbench-node/recordbench-unknown-service:1", "recordbench-unknown-service"),
        checker.Finding("recordbench-node/recordbench-unknown-service/recordbench-unknown-tool:1", "recordbench-unknown-tool"),
    ]


@pytest.mark.parametrize("relative", [
    "config/recordbench.env",
    "recordbench-node/config.py",
    "docs/assets/recordbench-historical/image.bin",
    "tests/recordbench-historical/tool",
    ".venv/recordbench-dependency/tool",
    "node_modules/recordbench-dependency/tool",
])
def test_declared_and_excluded_legacy_paths_remain_allowed(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.touch()
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize("prefix,suffix", [("my", ""), ("old-", ""), ("my", ".py")])
@pytest.mark.parametrize("directory", [False, True])
def test_prefixes_are_part_of_the_checked_path_component(tmp_path, prefix, suffix, directory):
    component = prefix + "record" + "bench" + suffix
    path = tmp_path / "tools" / component
    path.parent.mkdir()
    if directory:
        path.mkdir()
        (path / "config.py").touch()
    else:
        path.touch()
    assert checker.findings(tmp_path) == [checker.Finding(f"tools/{component}:1", component)]


@pytest.mark.parametrize("expression", [
    'f"{LEGACY_STEM}-unregistered-v99"',
    'LEGACY_STEM + "-unregistered-v99"',
    '"old-" + LEGACY_STEM',
    'f"{LEGACY_STEM}-unregistered-{version}"',
    'LEGACY_STEM + "-unregistered-" + version',
    '"{}-unregistered-v99".format(LEGACY_STEM)',
    '"%s-unregistered-v99" % LEGACY_STEM',
    'f"{LEGACY_STEM!r}-unregistered-v99"',
    '"%99999999999999999999s-unregistered-v99" % LEGACY_STEM',
])
def test_constant_legacy_compositions_require_a_contract(tmp_path, expression):
    path = tmp_path / "synthetic.py"
    path.write_text('from case_intelligence.legacy_names import LEGACY_STEM\nFORMAT = ' + expression + '\n')
    found = checker.findings(tmp_path)
    assert found
    assert all(item.location == "synthetic.py:2" for item in found)


def test_composed_declared_names_keep_their_contract_and_source_location(tmp_path):
    path = tmp_path / "synthetic.py"
    path.write_text('from case_intelligence.legacy_names import LEGACY_STEM\nFORMAT = f"{LEGACY_STEM}-exact-v1"\n')
    assert checker.findings(tmp_path) == []
    contracts = dict(NAME_CONTRACTS)
    del contracts["exact_v1"]
    assert checker.findings(tmp_path, contracts) == [
        checker.Finding("synthetic.py:2", "recordbench-exact-v1")
    ]


def test_composition_scan_tracks_import_and_assignment_aliases(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM as old\n'
        'stem: str = old\n'
        'FORMAT = f"{stem}-unregistered-v99"\n'
    )
    assert checker.findings(tmp_path) == [
        checker.Finding("synthetic.py:3", "recordbench-unregistered-v99")
    ]


@pytest.mark.parametrize("import_line,reference", [
    ('import case_intelligence.legacy_names as names', 'names.LEGACY_STEM'),
    ('from case_intelligence import legacy_names as names', 'names.LEGACY_STEM'),
    ('import case_intelligence.legacy_names', 'case_intelligence.legacy_names.LEGACY_STEM'),
])
def test_composition_scan_tracks_module_imports(tmp_path, import_line, reference):
    (tmp_path / "synthetic.py").write_text(import_line + '\nFORMAT = f"{' + reference + '}-unregistered-v99"\n')
    assert checker.findings(tmp_path) == [
        checker.Finding("synthetic.py:2", "recordbench-unregistered-v99")
    ]


def test_composition_scan_preserves_function_scope_and_never_executes_source(tmp_path):
    marker = tmp_path / "executed"
    (tmp_path / "synthetic.py").write_text(
        'from pathlib import Path\n'
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        f'Path({str(marker)!r}).touch()\n'
        'def local(LEGACY_STEM):\n'
        '    return f"{LEGACY_STEM}-unregistered-v99"\n'
        'def different():\n'
        '    LEGACY_STEM = "synthetic"\n'
        '    return f"{LEGACY_STEM}-unregistered-v99"\n'
        'FORMAT = f"{LEGACY_STEM}-unregistered-v99"\n'
    )
    assert checker.findings(tmp_path) == [
        checker.Finding("synthetic.py:9", "recordbench-unregistered-v99")
    ]
    assert not marker.exists()


def test_dynamic_composition_must_match_a_declared_family(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'unit = f"{LEGACY_STEM}-backup-{node_id}.service"\n'
        'browser = f"{LEGACY_STEM}:readiness:{state}"\n'
        'path = root / f"{LEGACY_STEM}.env"\n'
        'payload = json.dumps({"format": f"{LEGACY_STEM}-exact-v1"}) + "\\n"\n'
        'empty = ""\n'
        'format_id = f"{LEGACY_STEM}{empty}-exact-v1"\n'
    )
    assert checker.findings(tmp_path) == []


def test_mapping_interpolation_with_a_stem_requires_a_contract_key(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'FORMAT = "%(stem)s-unregistered-v99" % {"stem": LEGACY_STEM}\n'
    )
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("expression", [
    '"%(stem)s-unregistered-v99" % aliases',
    '"%s-unregistered-v99" % aliases',
    '"{}-unregistered-v99".format(aliases)',
    'f"{aliases}-unregistered-v99"',
])
def test_mapping_aliases_cannot_hide_a_stem_composition(tmp_path, expression):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'original = {"stem": LEGACY_STEM}\n'
        'aliases: dict = original\n'
        'FORMAT = ' + expression + '\n'
    )
    assert checker.findings(tmp_path)


def test_nested_functions_resolve_later_closure_aliases(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'def outer():\n'
        '    def build():\n'
        '        return f"{stem}-unregistered-v99"\n'
        '    stem = LEGACY_STEM\n'
        '    return build\n'
    )
    assert checker.findings(tmp_path) == [checker.Finding("synthetic.py:4", "recordbench-unregistered-v99")]


def test_deferred_functions_keep_declared_names_and_parameter_shadowing(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'class Reader:\n'
        '    def format(self):\n'
        '        return f"{stem}-exact-v1"\n'
        'def local(stem):\n'
        '    return f"{stem}-unregistered-v99"\n'
        'stem = LEGACY_STEM\n'
    )
    assert checker.findings(tmp_path) == []


def test_css_selectors_are_checked_as_text(tmp_path):
    (tmp_path / "synthetic.css").write_text(".recordbench-unregistered-v99 { display: block; }\n")
    assert checker.findings(tmp_path) == [checker.Finding("synthetic.css:1", ".recordbench-unregistered-v99")]


@pytest.mark.parametrize("definition", [
    'def build():\n    return f"{stem}-unregistered-v99"\n',
    'async def build():\n    return f"{stem}-unregistered-v99"\n',
    'build = lambda: f"{stem}-unregistered-v99"\n',
])
def test_function_bodies_resolve_later_module_aliases(tmp_path, definition):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n' + definition + 'stem = LEGACY_STEM\n'
    )
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("prefix", ["old-", "my"])
def test_literal_source_tokens_include_identifier_prefixes(tmp_path, prefix):
    identifier = prefix + "record" + "bench"
    (tmp_path / "synthetic.py").write_text(f'FORMAT = "{identifier}"\n')
    assert checker.findings(tmp_path) == [checker.Finding("synthetic.py:1", identifier)]


@pytest.mark.parametrize("filename", ["synthetic.css", "settings.ini", "tool.cfg", "config.txt", "install-tool"])
def test_operational_text_contents_are_checked_independently_of_suffix(tmp_path, filename):
    (tmp_path / filename).write_text("recordbench-unregistered-v99\n")
    assert checker.findings(tmp_path) == [checker.Finding(filename + ":1", "recordbench-unregistered-v99")]


@pytest.mark.parametrize("target_kind", ["file", "directory", "missing"])
def test_symlink_names_are_checked_without_following_targets(tmp_path, target_kind):
    hidden = tmp_path / "tests"
    hidden.mkdir()
    target = hidden / "target"
    if target_kind == "file":
        target.write_text("recordbench-unregistered-target\n")
    elif target_kind == "directory":
        target.mkdir()
        (target / "child.py").write_text("recordbench-unregistered-target\n")
    link = tmp_path / "recordbench-unregistered-link"
    link.symlink_to(target, target_is_directory=target_kind == "directory")
    assert checker.findings(tmp_path) == [checker.Finding(link.name + ":1", link.name)]


@pytest.mark.parametrize("identifier", [
    "RECORDBENCH_SYNTHETIC_SETTING",
    "X-RecordBench-Synthetic-Header",
    "recordbench.syntheticGitSetting",
    "config/recordbench.env",
    "/srv/recordbench/config",
    "/var/lib/recordbench-accounts/local-accounts.json",
    "recordbench-backup-0123456789.service",
    "recordbench-backup-@NODE_ID@.service",
    ".recordbench-managed-storage.json",
    "recordbench-exact-v1",
    "3.8.6+recordbench.1",
    "recordbench:assistant:conversation:",
])
def test_declared_alias_generation_and_retained_names_are_recognized(identifier):
    assert checker.scan_text("synthetic", identifier) == []


@pytest.mark.parametrize("key", sorted(NAME_CONTRACTS))
def test_each_declared_template_expands_to_a_recognized_identifier(key):
    template = NAME_CONTRACTS[key].legacy
    parts = {field: "SYNTHETIC" for _, field, _, _ in Formatter().parse(template) if field}
    value = template.format(**parts)
    assert checker.scan_text("synthetic", value) == []


@pytest.mark.parametrize("identifier", [
    "recordbench-unregistered-v1",
    "recordbench-qwen99-999b",
    "/var/lib/recordbench-unregistered/cache",
    "/srv/recordbench-unregistered/config",
    "recordbench-exact-v99",
])
def test_declared_roots_and_formats_do_not_allow_undeclared_siblings(identifier):
    assert checker.scan_text("synthetic", identifier)


def test_removing_registered_format_rejects_its_existing_use(tmp_path):
    (tmp_path / "reader.py").write_text('FORMAT = "recordbench-exact-v1"\n')
    assert checker.findings(tmp_path) == []
    contracts = dict(NAME_CONTRACTS)
    del contracts["exact_v1"]
    assert checker.findings(tmp_path, contracts) == [checker.Finding("reader.py:1", "recordbench-exact-v1")]


@pytest.mark.parametrize("invalid", [
    NameContract("", "exculpata-new-v1", "alias"),
    NameContract("recordbench-new-{suffix}", "exculpata-new", "alias"),
    NameContract("recordbench-new", "exculpata-new", "retained"),
])
def test_incomplete_or_inconsistent_fallback_table_is_rejected_before_scanning(tmp_path, invalid):
    contracts = dict(NAME_CONTRACTS, synthetic=invalid)
    found = checker.findings(tmp_path, contracts)
    assert found
    assert all(item.location == "src/case_intelligence/legacy_names.py" for item in found)
    assert all(item.identifier.startswith("synthetic:") for item in found)


@pytest.mark.parametrize("relative", [
    "tests/test_historical.py",
    "services/example/tests/test_historical.py",
    "docs/assets/historical.md",
    "CHANGELOG.md",
    ".venv/source.py",
    "__pycache__/cached.py",
    "node_modules/dependency.js",
])
def test_independent_historical_fixtures_and_caches_are_not_rewritten(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("recordbench-independent-historical-fixture\n")
    assert checker.findings(tmp_path) == []


def test_binary_assets_and_symlinks_are_not_followed(tmp_path):
    (tmp_path / "binary.py").write_bytes(b"\xff\0recordbench-binary-fixture")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nrecordbench-binary-fixture")
    hidden = tmp_path / "tests"
    hidden.mkdir()
    fixture = hidden / "historical.py"
    fixture.write_text("recordbench-independent-historical-fixture")
    (tmp_path / "linked.py").symlink_to(fixture)
    assert checker.findings(tmp_path) == [checker.Finding("binary.py:1", "unsupported undecodable operational source")]


def test_unknown_operational_json_identifier_fails(tmp_path):
    path = tmp_path / "config" / "models.json"
    path.parent.mkdir()
    path.write_text('{"format": "recordbench-unregistered-format-v99"}\n')
    assert checker.findings(tmp_path) == [
        checker.Finding("config/models.json:1", "recordbench-unregistered-format-v99")
    ]


@pytest.mark.parametrize("source", [
    'FORMAT = f"{str(LEGACY_STEM)}-unregistered-v99"',
    'FORMAT = f"{LEGACY_STEM.lower()}-unregistered-v99"',
    'FORMAT = f"{LEGACY_STEM.upper()}-unregistered-v99"',
    'values = {"stem": LEGACY_STEM}\nFORMAT = f"{values["stem"]}-unregistered-v99"',
    'values = {"stem": LEGACY_STEM}\nFORMAT = f"{values.get("stem")}-unregistered-v99"',
    'stem, other = LEGACY_STEM, "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'if condition:\n stem = LEGACY_STEM\nelse:\n stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'FORMAT = "".join([LEGACY_STEM, "-unregistered-v99"])',
    'FORMAT = "{stem}-unregistered-v99".format_map({"stem": LEGACY_STEM})',
    'stem = LEGACY_STEM\nstem += "-unregistered-v99"',
    'if (stem := LEGACY_STEM):\n FORMAT = f"{stem}-unregistered-v99"',
    'FORMAT = f"{name_value("stem", LEGACY_STEM)}-unregistered-v99"',
    'FORMAT = f"{text.replace("synthetic", LEGACY_STEM)}-unregistered-v99"',
])
def test_string_provenance_survives_expression_and_binding_forms(tmp_path, source):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM, name_value\n' + source + '\n'
    )
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'FORMAT = f"{LEGACY_STEM.lower()}-exact-v1"',
    'FORMAT = name_value("environment", LEGACY_STEM, suffix="")',
    'FORMAT = f"{name_value("stem", LEGACY_STEM)}-exact-v1"',
    'key = "synthetic"\nif condition:\n key = name_value("environment", LEGACY_STEM, suffix="TEST")\nFORMAT = f"{key}=value"',
    'value = json.dumps({"format": f"{LEGACY_STEM}-exact-v1"}) + "\\n"',
])
def test_string_provenance_preserves_declared_names_and_non_output_inputs(tmp_path, source):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM, name_value\n' + source + '\n'
    )
    assert checker.findings(tmp_path) == []


def test_tuple_assignment_resolves_all_sources_before_rebinding(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'stem, other = LEGACY_STEM, "synthetic"\n'
        'stem, other = other, stem\n'
        'FORMAT = f"{other}-unregistered-v99"\n'
    )
    assert checker.findings(tmp_path) == [checker.Finding("synthetic.py:4", "recordbench-unregistered-v99")]


@pytest.mark.parametrize("prefix,suffix", [("café", ""), ("cafe\u0301", ""), ("工具", ""), ("", "é")])
def test_unicode_identifier_parts_are_not_discarded(tmp_path, prefix, suffix):
    name = prefix + "record" + "bench" + suffix
    (tmp_path / "synthetic.py").write_text(f'FORMAT = "{name}"\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'stem = enabled and LEGACY_STEM\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = selected or LEGACY_STEM\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = enabled and (selected or LEGACY_STEM)\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = LEGACY_STEM\ndef build():\n return f"{stem}-unregistered-v99"\nFORMAT = build()\nstem = "synthetic"',
    'def build():\n return f"{stem}-unregistered-v99"\nstem = LEGACY_STEM\nFORMAT = build()\nstem = "synthetic"',
    'def outer():\n stem = LEGACY_STEM\n def build():\n  return f"{stem}-unregistered-v99"\n result = build()\n stem = "synthetic"\n return result',
    'stem = LEGACY_STEM\nbuild = lambda: f"{stem}-unregistered-v99"\nFORMAT = build()\nstem = "synthetic"',
])
def test_possible_conditional_and_prior_scope_bindings_are_checked(tmp_path, source):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n'
    )
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("name,shebang", [("tool", "#!/usr/bin/env python3\n"), ("tool", ""), ("tool.in", "#!/usr/bin/python3\n")])
def test_python_tools_are_detected_without_a_py_suffix(tmp_path, name, shebang):
    (tmp_path / name).write_text(shebang + 'from case_intelligence.legacy_names import LEGACY_STEM\nFORMAT = f"{LEGACY_STEM}-unregistered-v99"\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("suffix", [".css", ".txt", ""])
def test_decodable_text_is_checked_despite_nul_characters(tmp_path, suffix):
    (tmp_path / ("synthetic" + suffix)).write_bytes(b"\0.recordbench-unregistered-v99 { display: block; }\n")
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'stem = enabled and LEGACY_STEM\nFORMAT = f"{stem}-exact-v1"',
    'stem = selected or LEGACY_STEM\nFORMAT = f"{stem}-exact-v1"',
    'stem = LEGACY_STEM and "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = "synthetic" or LEGACY_STEM\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = LEGACY_STEM\ndef build():\n return f"{stem}-exact-v1"\nFORMAT = build()\nstem = "synthetic"',
    'stem = LEGACY_STEM\ndef outer(stem):\n def build():\n  return f"{stem}-unregistered-v99"\n return build()\nstem = "synthetic"',
    'stem = LEGACY_STEM\ndef outer():\n stem = "synthetic"\n def build():\n  return f"{stem}-unregistered-v99"\n return build()\nstem = "synthetic"',
    'stem = LEGACY_STEM\nstem = "synthetic"\ndef build():\n return f"{stem}-unregistered-v99"\nFORMAT = build()',
])
def test_possible_bindings_preserve_registered_names_and_shadowing(tmp_path, source):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n'
    )
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize("source", [
    '\ufefffrom case_intelligence.legacy_names import LEGACY_STEM\nFORMAT = f"{LEGACY_STEM}-unregistered-v99"\n',
    'FORMAT = "\\x72ecordbench-unregistered-v99"\n',
    'FORMAT = "\\u0072ecordbench-unregistered-v99"\n',
])
def test_python_decoding_does_not_hide_legacy_identifiers(tmp_path, source):
    (tmp_path / "synthetic.py").write_text(source)
    assert checker.findings(tmp_path)


def test_later_repeated_binding_remains_visible_to_deferred_body(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'stem = LEGACY_STEM\nstem = "synthetic"\n'
        'def build():\n return f"{stem}-unregistered-v99"\n'
        'stem = LEGACY_STEM\nFORMAT = build()\nstem = "synthetic"\n'
    )
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'settings.stem = LEGACY_STEM\nFORMAT = f"{settings.stem}-unregistered-v99"',
    'class Builder:\n def build(self):\n  self.stem = LEGACY_STEM\n  return f"{self.stem}-unregistered-v99"',
    'match selected:\n case "legacy":\n  stem = LEGACY_STEM\n case _:\n  stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'FORMAT = b"\\x72ecordbench-unregistered-v99"',
])
def test_remaining_binding_and_bytes_forms_keep_legacy_origins(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


def test_bom_prefixed_python_shebang_classifies_custom_suffix(tmp_path):
    (tmp_path / "tool.in").write_text('\ufeff#!/usr/bin/env python3\nfrom case_intelligence.legacy_names import LEGACY_STEM\nFORMAT = f"{LEGACY_STEM}-unregistered-v99"\n')
    assert checker.findings(tmp_path)


def test_class_namespace_is_not_a_method_enclosing_scope(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'stem = "synthetic"\nclass Builder:\n stem = LEGACY_STEM\n'
        ' def build(self):\n  return f"{stem}-unregistered-v99"\n'
    )
    found = checker.findings(tmp_path)
    assert len(found) == 1
    assert "legacy-bearing class namespace" in found[0].identifier
    # The method still uses the nonlegacy outer binding, not the class value.
    assert "unregistered-v99" not in found[0].identifier


@pytest.mark.parametrize("identifier", [
    "recordbench-backup-",
    "/var/lib/recordbench/recordbench-unregistered-v99",
    "/var/lib/recordbench-unregistered-v99/recordbench.env",
])
def test_operational_names_cannot_use_template_or_directory_exceptions(tmp_path, identifier):
    (tmp_path / "synthetic.py").write_text(f'FORMAT = "{identifier}"\n')
    assert checker.findings(tmp_path)


def test_authority_module_declarations_outside_table_are_checked(tmp_path):
    path = tmp_path / "src/case_intelligence/legacy_names.py"
    path.parent.mkdir(parents=True)
    path.write_text('OTHER = "recordbench-unregistered-v99"\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'match selected:\n case "legacy":\n  stem = LEGACY_STEM\n case _:\n  stem = "synthetic"\nFORMAT = f"{stem}-exact-v1"',
    'stem = LEGACY_STEM\nmatch selected:\n case _:\n  stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = LEGACY_STEM\nmatch selected:\n case {"stem": stem}:\n  FORMAT = f"{stem}-unregistered-v99"',
    'FORMAT = b"\\x72ecordbench-exact-v1"',
])
def test_attributes_match_cases_and_bytes_preserve_valid_sources(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path) == []


def test_methods_keep_the_actual_enclosing_legacy_binding(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'stem = LEGACY_STEM\nclass Builder:\n stem = "synthetic"\n'
        ' def build(self):\n  return f"{stem}-unregistered-v99"\n'
    )
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("value", [
    "/var/lib/recordbench/config/settings.json",
    "/var/lib/recordbench/config/recordbench.env",
    "recordbench-backup-@NODE_ID@.service",
    "recordbench-backup-{node_id}.{kind}",
    "recordbench:${name}",
    "recordbench://ticket/SYNTHETIC",
])
def test_complete_names_and_ordinary_descendants_keep_their_contracts(value):
    assert checker.scan_text("synthetic", value) == []


def test_namespace_prefix_does_not_accept_an_unknown_uri():
    assert checker.scan_text("synthetic", "recordbench://unregistered/SYNTHETIC")
    assert checker.scan_text("synthetic", "recordbench://")


def test_template_prefix_allowance_is_limited_to_documentation():
    assert checker.scan_text("synthetic.md", "recordbench-backup-") == []
    assert checker.scan_text("synthetic.py", "recordbench-backup-")


def test_authority_table_is_validated_separately_from_registered_exports(tmp_path):
    path = tmp_path / "src/case_intelligence/legacy_names.py"
    path.parent.mkdir(parents=True)
    path.write_text('NAME_CONTRACTS = {}\nOTHER = "recordbench-exact-v1"\n')
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize("source", [
    'stem = LEGACY_STEM\nmatch stem:\n case captured:\n  FORMAT = f"{captured}-unregistered-v99"',
    'match {"stem": LEGACY_STEM}:\n case {"stem": captured}:\n  FORMAT = f"{captured}-unregistered-v99"',
    'settings = SimpleNamespace()\nsettings.stem = "synthetic"\nclass Builder:\n settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings = SimpleNamespace()\nsettings.stem = "synthetic"\nclass Builder:\n settings.stem = LEGACY_STEM\nFORMAT = f"{settings.stem}-unregistered-v99"',
    'stem = b"\\x72ecordbench"\nFORMAT = stem + b"-unregistered-v99"',
    'stem = b"\\x72ecordbench"\nFORMAT = stem.decode("utf-8") + "-unregistered-v99"',
])
def test_capture_class_mutation_and_bytes_alias_provenance(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from types import SimpleNamespace\nfrom case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'NAME_CONTRACTS = OTHER = {"key": "recordbench-unregistered-v99"}\n',
    'NAME_CONTRACTS = {}; OTHER = "recordbench-unregistered-v99"\n',
    'NAME_CONTRACTS = {}; OTHER = "\\x72ecordbench-unregistered-v99"\n',
    'NAME_CONTRACTS = {\n}; OTHER = "\\x72ecordbench-unregistered-v99"\n',
    'é = "synthetic"; NAME_CONTRACTS = {}; OTHER = "\\x72ecordbench-unregistered-v99"\n',
])
def test_authority_mask_preserves_adjacent_statements(tmp_path, source):
    path = tmp_path / "src/case_intelligence/legacy_names.py"
    path.parent.mkdir(parents=True)
    path.write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("suffix", ["?bad=query", "#fragment", "%2Fextra"])
def test_complete_uri_suffixes_are_checked(suffix):
    assert checker.scan_text("synthetic", "recordbench://ticket/SYNTHETIC" + suffix)


@pytest.mark.parametrize("source", [
    'match [LEGACY_STEM]:\n case [captured]:\n  FORMAT = f"{captured}-unregistered-v99"',
    'match ["synthetic", LEGACY_STEM]:\n case [_, *captured]:\n  FORMAT = "".join(captured) + "-unregistered-v99"',
    'match {"stem": LEGACY_STEM}:\n case {**captured}:\n  FORMAT = captured["stem"] + "-unregistered-v99"',
    'match [LEGACY_STEM]:\n case [captured] | (captured,):\n  FORMAT = f"{captured}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n def build(self):\n  return f"{settings.stem}-unregistered-v99"\n settings.stem = LEGACY_STEM',
    'def outer():\n settings.stem = "synthetic"\n class Builder:\n  settings.stem = LEGACY_STEM\n  def build(self):\n   return f"{settings.stem}-unregistered-v99"',
    'stem = LEGACY_STEM.encode("utf-8")\nFORMAT = stem + b"-unregistered-v99"',
    'stem = LEGACY_STEM.encode("unknown")\nFORMAT = stem + b"-unregistered-v99"',
])
def test_nested_capture_mutation_and_bytes_provenance(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'match LEGACY_STEM:\n case captured:\n  FORMAT = f"{captured}-exact-v1"',
    'match {"stem": LEGACY_STEM, "safe": "synthetic"}:\n case {"safe": captured}:\n  FORMAT = f"{captured}-unregistered-v99"',
    'match [LEGACY_STEM, "synthetic"]:\n case [_, captured]:\n  FORMAT = f"{captured}-unregistered-v99"',
    'match {"stem": LEGACY_STEM, "safe": "synthetic"}:\n case {"stem": _, **captured}:\n  FORMAT = captured["safe"] + "-unregistered-v99"',
    'match [LEGACY_STEM, "synthetic"]:\n case [_, *captured]:\n  FORMAT = "".join(captured) + "-unregistered-v99"',
    'stem = b"\\x72ecordbench"\nFORMAT = stem + b"-exact-v1"',
    'stem = b"\\x72ecordbench"\nFORMAT = stem.decode(encoding="utf-8") + "-exact-v1"',
    'stem = LEGACY_STEM.encode("ascii")\nFORMAT = stem + b"-exact-v1"',
])
def test_captures_class_shadowing_and_bytes_keep_valid_names(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from types import SimpleNamespace\nfrom case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path) == []


def test_authority_span_keeps_valid_adjacent_statements(tmp_path):
    path = tmp_path / "src/case_intelligence/legacy_names.py"
    path.parent.mkdir(parents=True)
    path.write_text('é = "synthetic"; NAME_CONTRACTS = {}; OTHER = "recordbench-exact-v1"\n')
    assert checker.findings(tmp_path) == []


def test_malformed_uri_exception_is_exact():
    assert checker.scan_text("synthetic", "recordbench://ticket/bad?query") == []
    assert checker.scan_text("synthetic", "recordbench://ticket/bad?query#extra")
    assert checker.scan_text("synthetic", "recordbench://ticket/other?query")


@pytest.mark.parametrize("source", [
    'settings.stem = "synthetic"\nclass Builder:\n if selected:\n  settings.stem = LEGACY_STEM\n else:\n  settings.stem = "synthetic"\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n if selected:\n  settings = unknown\n settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'stem = LEGACY_STEM\nmatch {"first":"synthetic", "second":stem}:\n case {"first":stem,"second":captured}:\n  FORMAT=f"{captured}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n match selected:\n  case "legacy":\n   settings.stem = LEGACY_STEM\n  case _:\n   settings.stem = "synthetic"\nFORMAT = f"{settings.stem}-unregistered-v99"',
])
def test_branch_mutations_and_simultaneous_captures_preserve_provenance(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'stem = "synthetic"\nmatch {"first":LEGACY_STEM, "second":stem}:\n case {"first":stem,"second":captured}:\n  FORMAT=f"{captured}-unregistered-v99"',
])
def test_branch_overwrites_and_simultaneous_captures_keep_valid_names(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize("source", [
    'settings = "synthetic"\nclass Builder:\n global settings\n settings = LEGACY_STEM\n def build(self):\n  return f"{settings}-unregistered-v99"',
    'def outer():\n settings = "synthetic"\n class Builder:\n  nonlocal settings\n  settings = LEGACY_STEM\n  def build(self):\n   return f"{settings}-unregistered-v99"',
    'settings = {}\nclass Builder:\n settings["stem"] = LEGACY_STEM\n def build(self):\n  return settings["stem"] + "-unregistered-v99"',
    'settings = ["synthetic"]\nclass Builder:\n settings[0] = LEGACY_STEM\n def build(self):\n  return settings[0] + "-unregistered-v99"',
    '*captured, tail = (LEGACY_STEM, "synthetic")\nFORMAT = captured[0] + "-unregistered-v99"',
    'first, *captured, tail = ("synthetic", LEGACY_STEM, "synthetic")\nFORMAT = captured[0] + "-unregistered-v99"',
    'try:\n stem = LEGACY_STEM\nexcept Exception:\n stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = LEGACY_STEM\nfor item in []:\n stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = LEGACY_STEM\nwhile selected:\n stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'async def build():\n stem = LEGACY_STEM\n async for item in selected:\n  stem = "synthetic"\n return f"{stem}-unregistered-v99"',
])
def test_declared_scopes_container_writes_and_control_paths_are_checked(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'parts = (LEGACY_STEM, "synthetic")\nmatch parts:\n case [_, captured]:\n  FORMAT = f"{captured}-unregistered-v99"',
    'parts = {"stem":LEGACY_STEM, "safe":"synthetic"}\nmatch parts:\n case {"safe": captured}:\n  FORMAT = f"{captured}-unregistered-v99"',
])
def test_container_capture_aliases_and_implicit_class_locals_stay_valid(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize("suffix", ["(extra)", "'extra'", "*extra"])
def test_uri_exception_does_not_hide_query_subdelimiters(suffix):
    assert checker.scan_text("synthetic", "recordbench://ticket/bad?query" + suffix)


@pytest.mark.parametrize("source", [
    'settings = {"stem":"synthetic"}\nalias = settings\nclass Builder:\n settings["stem"] = LEGACY_STEM\n def build(self):\n  return alias["stem"] + "-unregistered-v99"',
    'settings = ["synthetic"]\nalias = settings\nclass Builder:\n settings[0] = LEGACY_STEM\n def build(self):\n  return alias[0] + "-unregistered-v99"',
    'settings = {}\nsettings[index] = LEGACY_STEM\nFORMAT = settings["stem"] + "-unregistered-v99"',
    'parts = (LEGACY_STEM, "synthetic")\nmatch parts:\n case [captured, _]:\n  FORMAT = f"{captured}-unregistered-v99"',
    'parts = {"stem":LEGACY_STEM, "safe":"synthetic"}\nmatch parts:\n case {"stem": captured}:\n  FORMAT = f"{captured}-unregistered-v99"',
    'stem = "synthetic"\ntry:\n stem = LEGACY_STEM\n operation()\n stem = "synthetic"\nexcept Exception:\n FORMAT = f"{stem}-unregistered-v99"',
    'stem = "synthetic"\ntry:\n operation()\nexcept Exception:\n stem = LEGACY_STEM\nelse:\n stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = LEGACY_STEM\nfor item in selected:\n break\nelse:\n stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'parts = ("recordbench-unregistered-v99", "synthetic")\nmatch parts:\n case [captured, _]:\n  FORMAT = captured',
])
def test_container_and_control_flow_uncertainty_keeps_legacy_origin(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'first, *captured = (LEGACY_STEM, "synthetic")\nFORMAT = captured[0] + "-unregistered-v99"',
    'first, *captured, tail = (LEGACY_STEM, "synthetic", LEGACY_STEM)\nFORMAT = captured[0] + "-unregistered-v99"',
    'try:\n stem = LEGACY_STEM\nexcept Exception:\n stem = LEGACY_STEM\nfinally:\n stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'try:\n stem = LEGACY_STEM\n stem = "synthetic"\nexcept Exception:\n stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = LEGACY_STEM\nfor item in selected:\n stem = "synthetic"\nstem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
])
def test_explicit_overwrites_and_safe_members_remain_valid(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize("source", [
    'for stem in [LEGACY_STEM]:\n FORMAT = f"{stem}-unregistered-v99"',
    'parts = [LEGACY_STEM, "synthetic"]\nfor stem in parts:\n FORMAT = f"{stem}-unregistered-v99"',
    'stem = "synthetic"\ntry:\n operation()\nexcept* ValueError:\n stem = LEGACY_STEM\nexcept* TypeError:\n FORMAT = f"{stem}-unregistered-v99"',
])
def test_known_iteration_and_exception_group_origins_are_not_cleared(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'for stem in [LEGACY_STEM]:\n FORMAT = f"{stem}-exact-v1"',
    'for stem in {"synthetic": LEGACY_STEM}:\n FORMAT = f"{stem}-unregistered-v99"',
])
def test_known_iteration_values_keep_valid_names(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path) == []



def test_dynamic_row_selection_keeps_destructured_member_positions(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'fields = {"first": [("synthetic", "prompt", LEGACY_STEM), ("safe", LEGACY_STEM, "default")], "second": [("also-safe", "prompt", "default")]}\n'
        'for name, prompt, default in fields[selected]:\n FORMAT = "CASE_INTELLIGENCE_" + name.upper()\n'
    )
    assert checker.findings(tmp_path) == []


def test_dynamic_row_selection_retains_a_legacy_name_member(tmp_path):
    (tmp_path / "synthetic.py").write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM\n'
        'fields = {"first": [(LEGACY_STEM, "synthetic")], "second": [("synthetic", "safe")]}\n'
        'for name, unused in fields[selected]:\n FORMAT = f"{name}-unregistered-v99"\n'
    )
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("mutation", [None, "count", "body", "literal"])
def test_analysis_helper_disposition_is_exact_and_keeps_literal_scanning(tmp_path, mutation):
    import ast
    source = (ROOT / "scripts/doc_identifier_check.py").read_text()
    module = ast.parse(source)
    helper = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "_swap")
    source = "from case_intelligence.legacy_names import *\n" + ast.get_source_segment(source, helper) + "\n"
    if mutation == "count":
        source = source.replace("token.replace(old, new, 1)", "token.replace(old, new, 2)")
    elif mutation == "body":
        source = source.replace('    return None', '    unused = "synthetic"\n    return None')
    elif mutation == "literal":
        source += 'OTHER = "recordbench-unregistered-v99"\n'
    path = tmp_path / "scripts/doc_identifier_check.py"
    path.parent.mkdir()
    path.write_text(source)
    assert bool(checker.findings(tmp_path)) is (mutation is not None)


def test_analysis_helper_disposition_does_not_follow_another_path(tmp_path):
    import ast
    source = (ROOT / "scripts/doc_identifier_check.py").read_text()
    helper = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "_swap")
    (tmp_path / "synthetic.py").write_text("from case_intelligence.legacy_names import *\n" + ast.get_source_segment(source, helper) + "\n")
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'parts = [LEGACY_STEM]\ncopied = [*parts]\nFORMAT = copied[0] + "-unregistered-v99"',
    'settings = {"nested":{"stem":"synthetic"}}\nalias = settings["nested"]\nalias["stem"] = LEGACY_STEM\nFORMAT = settings["nested"]["stem"] + "-unregistered-v99"',
    'stem = "synthetic"\nfor item in ["synthetic"]:\n stem = LEGACY_STEM\n break\n stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = "synthetic"\nfor item in ["synthetic"]:\n stem = LEGACY_STEM\n continue\n stem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
    'a = b = {"stem":"synthetic"}\na["stem"] = LEGACY_STEM\nFORMAT = b["stem"] + "-unregistered-v99"',
    'a = b = ["synthetic"]\na[0] = LEGACY_STEM\nFORMAT = b[0] + "-unregistered-v99"',
    'settings = "synthetic"\ndef outer():\n global settings\n settings = "synthetic"\n class Builder:\n  global settings\n  settings = LEGACY_STEM\n  def build(self):\n   return f"{settings}-unregistered-v99"',
    'settings = {LEGACY_STEM:"synthetic"}\nfor stem in settings:\n FORMAT = f"{stem}-unregistered-v99"',
    'stem = "synthetic"\nvalues = [f"{stem}-unregistered-v99" for stem in [LEGACY_STEM]]',
    'stem = "synthetic"\nvalues = {f"{stem}-unregistered-v99" for stem in [LEGACY_STEM]}',
    'stem = "synthetic"\nvalues = {stem:f"{stem}-unregistered-v99" for stem in [LEGACY_STEM]}',
    'stem = "synthetic"\nvalues = (f"{stem}-unregistered-v99" for stem in [LEGACY_STEM])',
    'stem = "synthetic"\ntry:\n if selected:\n  stem = LEGACY_STEM\n  operation()\n  stem = "synthetic"\nexcept Exception:\n pass\nFORMAT = f"{stem}-unregistered-v99"',
    'captured = "synthetic"\nmatch LEGACY_STEM:\n case captured if selected:\n  captured = "synthetic"\nFORMAT = f"{captured}-unregistered-v99"',
    'stem = "synthetic"\n@deco(stem := LEGACY_STEM)\nclass Builder:\n pass\nFORMAT = f"{stem}-unregistered-v99"',
])
def test_reviewed_unsupported_or_undeclared_forms_fail_closed(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'parts = ["synthetic", LEGACY_STEM]\nFORMAT = parts[-2] + "-unregistered-v99"',
    'from other_module import LEGACY_STEM\nFORMAT = f"{LEGACY_STEM}-unregistered-v99"',
    'stem = LEGACY_STEM\nvalues = [f"{stem}-unregistered-v99" for stem in ["synthetic"]]',
    'stem = LEGACY_STEM\nvalues = {f"{stem}-unregistered-v99" for stem in ["synthetic"]}',
    'stem = LEGACY_STEM\nvalues = {stem:f"{stem}-unregistered-v99" for stem in ["synthetic"]}',
    'stem = LEGACY_STEM\nvalues = (f"{stem}-unregistered-v99" for stem in ["synthetic"])',
])
def test_reviewed_safe_members_and_foreign_scopes_do_not_inherit_origins(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path) == []


def test_reviewed_helper_fingerprint_is_independent_of_ast_schema(monkeypatch, tmp_path):
    import ast
    source = (ROOT / "scripts/doc_identifier_check.py").read_text()
    helper = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "_swap")
    source = 'from case_intelligence.legacy_names import *\n' + ast.get_source_segment(source, helper) + '\n'
    path = tmp_path / "scripts/doc_identifier_check.py"
    path.parent.mkdir()
    path.write_text(source)
    original = checker.ast.dump
    monkeypatch.setattr(checker.ast, "dump", lambda *a, **kw: original(*a, **kw) + ",new_optional_ast_field=[]")
    assert checker.findings(tmp_path) == []


# The approved grammar rejects these formerly modeled mutations at their source,
# even where a later overwrite could make a particular runtime path safe.
@pytest.mark.parametrize("source", [
    'settings.stem = LEGACY_STEM\nFORMAT = f"{settings.stem}-exact-v1"',
    'settings.stem = LEGACY_STEM\nsettings.stem = "synthetic"\nFORMAT = f"{settings.stem}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n settings = SimpleNamespace()\n settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n import synthetic as settings\n settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n from synthetic import settings\n settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = LEGACY_STEM\nclass Builder:\n settings.stem = "synthetic"\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n if selected:\n  settings = first\n else:\n  settings = second\n settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = LEGACY_STEM\nclass Builder:\n if selected:\n  settings.stem = "synthetic"\n else:\n  settings.stem = "also-synthetic"\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n if selected:\n  settings.stem = LEGACY_STEM\n else:\n  settings.stem = "synthetic"\n def build(self):\n  return f"{settings.stem}-exact-v1"',
    'settings.stem = "synthetic"\nclass Builder:\n with context as settings:\n  settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n try:\n  operation()\n except Exception as settings:\n  settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings = "synthetic"\ndef outer():\n settings = "synthetic-local"\n class Builder:\n  global settings\n  settings = LEGACY_STEM\n  def build(self):\n   return f"{settings}-unregistered-v99"',
    'settings = {"stem":LEGACY_STEM}\nalias = settings\nclass Builder:\n alias["stem"] = "synthetic"\n def build(self):\n  return settings["stem"] + "-unregistered-v99"',
    'settings = [LEGACY_STEM]\nalias = settings\nclass Builder:\n alias[0] = "synthetic"\n def build(self):\n  return settings[0] + "-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n with context as settings:\n  settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-exact-v1"',
])
def test_mutable_and_outer_writes_require_explicit_review(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from types import SimpleNamespace\nfrom case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    found = checker.findings(tmp_path)
    assert any("unsupported legacy syntax" in item.identifier for item in found)


@pytest.mark.parametrize("source", [
    'stem = LEGACY_STEM\nFORMAT = f"{stem}-exact-v1"',
    'settings = {"stem": LEGACY_STEM}\nFORMAT = settings["stem"] + "-exact-v1"',
    'stem = LEGACY_STEM\nstem = "synthetic"\nFORMAT = f"{stem}-unregistered-v99"',
])
def test_immutable_alternatives_to_mutation_remain_supported(tmp_path, source):
    (tmp_path / "synthetic.py").write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize("source", [
    'value = reader(LEGACY_STEM)\nFORMAT = f"{value}-synthetic"',
    'parser = Parser(prog=LEGACY_STEM)\nFORMAT = f"{parser.read()}-synthetic"',
])
def test_opaque_helper_results_keep_known_legacy_origins(tmp_path, source):
    (tmp_path / "synthetic.py").write_text("from case_intelligence.legacy_names import LEGACY_STEM\n" + source + "\n")
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'value = text.removeprefix(LEGACY_STEM)\nFORMAT = f"{value}-synthetic"',
    'value = text.replace(LEGACY_STEM, "synthetic")\nFORMAT = f"{value}-synthetic"',
    'value = mapping.get(LEGACY_STEM)\nFORMAT = f"{value}-synthetic"',
    'import argparse\nparser = argparse.ArgumentParser(prog=LEGACY_STEM)\nFORMAT = f"{parser.prog}-unregistered-v99"',
    'import re\npattern = re.compile(LEGACY_STEM)\nFORMAT = f"{pattern.pattern}-unregistered-v99"',
    'def len(value):\n return value\nFORMAT = f"{len(LEGACY_STEM)}-unregistered-v99"',
])
def test_unverified_consumers_and_exposed_input_attributes_fail_closed(tmp_path, source):
    (tmp_path / "synthetic.py").write_text("from case_intelligence.legacy_names import LEGACY_STEM\n" + source + "\n")
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source", [
    'settings = "synthetic"\ndef configure():\n global settings\n settings = LEGACY_STEM',
    'def outer():\n settings = "synthetic"\n def configure():\n  nonlocal settings\n  settings = LEGACY_STEM',
    'keys = {LEGACY_STEM: "synthetic"}\nvalue = reader(keys)\nFORMAT = f"{value}-unregistered-v99"',
])
def test_outer_function_writes_and_mapping_keys_preserve_restriction(tmp_path, source):
    (tmp_path / "synthetic.py").write_text("from case_intelligence.legacy_names import LEGACY_STEM\n" + source + "\n")
    assert checker.findings(tmp_path)


@pytest.mark.parametrize("source, blocked", [
    ('from case_intelligence.compat_env import env\nvalue = env(LEGACY_STEM, "synthetic")\nFORMAT = f"{value}-unregistered-v99"', False),
    ('from case_intelligence.compat_env import env as read\nvalue = read("SYNTHETIC", LEGACY_STEM)\nFORMAT = f"{value}-unregistered-v99"', True),
    ('from case_intelligence.compat_env import required_env\nvalue = required_env("SYNTHETIC", environ={"SYNTHETIC": LEGACY_STEM})\nFORMAT = f"{value}-unregistered-v99"', True),
    ('from other_module import env\nvalue = env(LEGACY_STEM)\nFORMAT = f"{value}-unregistered-v99"', True),
    ('import case_intelligence.compat_env as config\nconfig.env = foreign\nvalue = config.env(LEGACY_STEM)\nFORMAT = f"{value}-unregistered-v99"', True),
    ('mapping = {LEGACY_STEM: "synthetic"}\nvalue = mapping.get(key)\nFORMAT = f"{value}-unregistered-v99"', False),
    ('mapping = {"synthetic": LEGACY_STEM}\nvalue = mapping.get(key)\nFORMAT = f"{value}-unregistered-v99"', True),
    ('from pathlib import Path\nvalue = Path(name_value("app_env", LEGACY_STEM)).read_text()\nFORMAT = f"{value}-unregistered-v99"', False),
    ('from foreign import Path\nvalue = Path(LEGACY_STEM).read_text()\nFORMAT = f"{value}-unregistered-v99"', True),
    ('values = [item for item in ["synthetic", LEGACY_STEM] if selected]\nFORMAT = f"{values[0]}-unregistered-v99"', True),
    ('values = [item for item in [LEGACY_STEM] if foreign(item)]', True),
    ('values = dict(stem=LEGACY_STEM)\ncopy = dict(values, other="synthetic")\nFORMAT = f"{copy["stem"]}-unregistered-v99"', True),
    ('values = set()\nvalues = values | {LEGACY_STEM}\nfor stem in values:\n FORMAT = f"{stem}-unregistered-v99"', True),
])
def test_declared_helper_and_pure_container_roles(tmp_path, source, blocked):
    (tmp_path / 'synthetic.py').write_text(
        'from case_intelligence.legacy_names import LEGACY_STEM, name_value\n' + source + '\n'
    )
    assert bool(checker.findings(tmp_path)) is blocked


def test_environment_selector_contract_requires_unchanged_implementation(tmp_path, monkeypatch):
    implementation = tmp_path / 'src/case_intelligence/compat_env.py'
    implementation.parent.mkdir(parents=True)
    implementation.write_text('def env(name, default=None):\n return name\n')
    monkeypatch.setattr(checker, 'ROOT', tmp_path)
    found = checker.scan_python('synthetic.py',
        'from case_intelligence.compat_env import env\nvalue = env("SYNTHETIC")\n', NAME_CONTRACTS)
    assert any('changed environment selector implementation' in item.identifier for item in found)


def test_source_form_receipts_are_exact_and_cannot_hide_names():
    import hashlib
    text = 'synthetic source\n'
    location = 'src/synthetic.py'
    diagnostic = 'unsupported legacy composition; use a contract key'
    records = {location: {'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
                         'diagnostics': [{'line': 2, 'identifier': diagnostic}]}}
    allowed = checker.Finding(location + ':2', diagnostic)
    extra = checker.Finding(location + ':3', diagnostic)
    literal = checker.Finding(location + ':2', 'recordbench-unregistered-v99')
    assert checker.apply_source_forms(location, text, [allowed, extra, literal], records) == [extra, literal]
    assert checker.apply_source_forms('other.py', text, [allowed], records) == [allowed]
    assert checker.apply_source_forms(location, text + '# changed\n', [allowed], records) == [allowed]
    found = checker.apply_source_forms(location, text + '# changed\n', [], records, require_current=True)
    assert len(found) == 1 and 'explicit review required' in found[0].identifier


def test_source_form_contracts_never_accept_literal_diagnostics(tmp_path, monkeypatch):
    import json
    source = tmp_path / 'forms.json'
    source.write_text(json.dumps({'version': 1, 'files': {'synthetic.py': {
        'source_sha256': 'a' * 64, 'diagnostics': [{'line': 1, 'identifier': 'recordbench-unregistered-v99',
                                                 'reason': 'synthetic', 'validation': 'synthetic'}]}}}))
    monkeypatch.setattr(checker, 'SOURCE_FORM_CONTRACT_PATH', source)
    with pytest.raises(ValueError, match='diagnostic contract'):
        checker.source_form_contracts()


@pytest.mark.parametrize('source', [
    'stem = "synthetic"\n[item for item in ["one"] if (stem := LEGACY_STEM)]\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = "synthetic"\n[(stem := LEGACY_STEM) for item in ["one"]]\nFORMAT = f"{stem}-unregistered-v99"',
    'stem = LEGACY_STEM\nclass C:\n stem = "synthetic"\n values = [f"{stem}-unregistered-v99" for item in ["one"]]',
    'stem = "synthetic"\nvalues = (f"{stem}-unregistered-v99" for item in ["one"])\nstem = LEGACY_STEM\nlist(values)',
    'class C:\n stem = LEGACY_STEM\nFORMAT = C.stem + "-unregistered-v99"',
    'def build(stem=LEGACY_STEM):\n return f"{stem}-unregistered-v99"\nbuild()',
    'def build(*, stem=LEGACY_STEM):\n return f"{stem}-unregistered-v99"\nbuild()',
    'build = lambda stem=LEGACY_STEM: f"{stem}-unregistered-v99"\nbuild()',
    'stem = LEGACY_STEM\nstem: str\nFORMAT = f"{stem}-unregistered-v99"',
    'def build():\n stem = LEGACY_STEM\n stem: str\n return f"{stem}-unregistered-v99"',
])
def test_final_review_scope_and_default_origins(tmp_path, source):
    (tmp_path / 'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('relative, statement', [
    ('src/case_intelligence/store/tool.py', 'from ..legacy_names import LEGACY_STEM'),
    ('src/case_intelligence/store/deeper/tool.py', 'from ...legacy_names import LEGACY_STEM'),
    ('src/case_intelligence/store/tool.py', 'from .. import legacy_names as names'),
])
def test_final_review_nested_authority_imports(tmp_path, relative, statement):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    stem = 'names.LEGACY_STEM' if ' as names' in statement else 'LEGACY_STEM'
    path.write_text(statement + '\nFORMAT = f"{' + stem + '}-unregistered-v99"\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('suffix', ['.js', '.cjs', '.mjs'])
@pytest.mark.parametrize('escape', [r'\x72', r'\u0072', r'\u{72}'])
def test_final_review_javascript_escaped_literals(tmp_path, suffix, escape):
    (tmp_path / ('synthetic' + suffix)).write_text('const FORMAT = "' + escape + 'ecordbench-unregistered-v99";\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('extra', ['+unregistered-v99', ':unregistered-v99', '%unregistered-v99'])
def test_final_review_path_punctuation(tmp_path, extra):
    (tmp_path / 'synthetic.py').write_text('PATH = "/var/lib/' + 'record' + 'bench' + extra + '"\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('prefix,suffix', [('', '.'), ('.', ''), ('...', '')])
def test_final_review_exact_literal_dots(tmp_path, prefix, suffix):
    (tmp_path / 'synthetic.py').write_text('FORMAT = "' + prefix + 'record' + 'bench-exact-v1' + suffix + '"\n')
    assert checker.findings(tmp_path)


def test_final_review_missing_receipt_source(tmp_path, monkeypatch):
    import json
    path = tmp_path / 'forms.json'
    path.write_text(json.dumps({'version': 1, 'files': {'missing.py': {
        'source_sha256': 'a' * 64, 'diagnostics': [{'line': 1,
        'identifier': 'unsupported legacy composition; use a contract key',
        'reason': 'synthetic fixture', 'validation': 'synthetic fixture'}]}}}))
    monkeypatch.setattr(checker, 'ROOT', tmp_path)
    monkeypatch.setattr(checker, 'SOURCE_FORM_CONTRACT_PATH', path)
    assert any('unused source-form' in item.identifier for item in checker.findings(tmp_path))


@pytest.mark.parametrize('source', [
    'stem = LEGACY_STEM\nclass C:\n stem = "synthetic"\n values = [f"{stem}-unregistered-v99" for item in unknown]',
    'stem = "synthetic"\nvalues = (f"{stem}-unregistered-v99" for item in ["one"])\nstem = LEGACY_STEM\nlist(values)\nstem = "synthetic"',
    'stem = LEGACY_STEM\ndef build(value=stem):\n return f"{value}-unregistered-v99"\nstem = "synthetic"\nbuild()',
    'class C:\n stem = LEGACY_STEM\n class D:\n  value = LEGACY_STEM\nFORMAT = C.D.value + "-unregistered-v99"',
])
def test_final_review_deferred_and_nested_scope_controls(tmp_path, source):
    (tmp_path / 'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'stem = "synthetic"\ndef build(value=stem):\n return f"{value}-unregistered-v99"\nstem = LEGACY_STEM\nbuild()',
    'stem = "synthetic"\nclass C:\n stem = LEGACY_STEM\n values = [f"{stem}-unregistered-v99" for item in ["one"]]',
    'class C:\n stem = LEGACY_STEM\n stem = "synthetic"\nFORMAT = C.stem + "-unregistered-v99"',
    'stem = LEGACY_STEM\nvalues = (f"{stem}-exact-v1" for item in ["one"])',
])
def test_final_review_precise_safe_controls(tmp_path, source):
    (tmp_path / 'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    found = checker.findings(tmp_path)
    if "class C" in source and 'stem = "synthetic"\nFORMAT' not in source:
        assert any("legacy-bearing class namespace" in item.identifier for item in found)
    elif "values = (" in source:
        assert any("deferred legacy generator" in item.identifier for item in found)
    else:
        assert found == []


def test_final_review_js_comments_do_not_hide_escaped_literals(tmp_path):
    source = '// synthetic apostrophe: \'\nconst FORMAT = "\\x72ecordbench-unregistered-v99";\n// another apostrophe: \'\n'
    (tmp_path / 'synthetic.js').write_text(source)
    assert checker.findings(tmp_path)


def test_final_review_js_escaped_backslash_does_not_decode_twice(tmp_path):
    (tmp_path / 'synthetic.js').write_text(r'const text = "\\x72ecordbench-unregistered-v99";' + '\n')
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize('relative, content', [
    ('synthetic.js', '// A registered prose name ends a sentence: recordbench-exact-v1.\nconst x = state.recordbenchSourceNote;'),
    ('compose.yaml', 'volumes:\n  - ${SYNTHETIC_ROOT}:/run/recordbench-secrets:ro\n'),
    ('compose.yaml', 'tmpfs:\n  - /run/recordbench-auth:rw,noexec,nosuid,nodev,size=2m\n'),
])
def test_final_review_source_syntax_is_distinct_from_literal_names(tmp_path, relative, content):
    (tmp_path / relative).write_text(content)
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize('value', ['/run/recordbench-secrets:ro', 'state.recordbenchSourceNote'])
def test_final_review_source_syntax_does_not_exempt_decoded_literals(tmp_path, value):
    (tmp_path / 'synthetic.py').write_text('value = ' + repr(value) + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('state', ['missing', 'renamed', 'nonpython', 'binary', 'excluded'])
def test_final_review_unused_receipt_variants(tmp_path, monkeypatch, state):
    import hashlib
    import json
    source = 'synthetic = True\n'
    path = {'renamed': 'other.py', 'nonpython': 'source.txt', 'binary': 'source.py', 'excluded': 'tests/source.py'}.get(state)
    recorded = 'source.txt' if state == 'nonpython' else 'tests/source.py' if state == 'excluded' else 'source.py'
    if path:
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'\xff' if state == 'binary' else source.encode())
    manifest = tmp_path / 'forms.json'
    manifest.write_text(json.dumps({'version': 1, 'files': {recorded: {
        'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'diagnostics': [{'line': 1, 'identifier': 'unsupported legacy composition; use a contract key',
                         'reason': 'synthetic', 'validation': 'synthetic'}]}}}))
    monkeypatch.setattr(checker, 'ROOT', tmp_path)
    monkeypatch.setattr(checker, 'SOURCE_FORM_CONTRACT_PATH', manifest)
    assert any('unused source-form' in item.identifier for item in checker.findings(tmp_path))


def test_final_review_js_regex_comment_ambiguity_does_not_hide_escapes(tmp_path):
    (tmp_path / 'synthetic.js').write_text(r'const regex = /\//; const FORMAT = "\x72ecordbench-unregistered-v99";' + '\n')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source, expected', [
    ('value = LEGACY_STEM\nfor _ in range(2):\n value += ".env"\nFORMAT = value', 'record' + 'bench.env.env'),
    ('value = LEGACY_STEM\nfor _ in [1, 2]:\n value = value + ".env"\nFORMAT = value', 'record' + 'bench.env.env'),
    ('value = LEGACY_STEM\ni = 0\nwhile i < 2:\n value += ".env"\n i += 1\nFORMAT = value', 'record' + 'bench.env.env'),
    ('stem = LEGACY_STEM\nFalse and (stem := "synthetic")\nFORMAT = f"{stem}-unregistered-v99"', 'record' + 'bench-unregistered-v99'),
    ('stem = LEGACY_STEM\nTrue or (stem := "synthetic")\nFORMAT = f"{stem}-unregistered-v99"', 'record' + 'bench-unregistered-v99'),
    ('stem = LEGACY_STEM\n(stem := "synthetic") if False else None\nFORMAT = f"{stem}-unregistered-v99"', 'record' + 'bench-unregistered-v99'),
])
def test_independent_review_repeated_and_conditional_writes(tmp_path, source, expected):
    source = 'from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n'
    # These fixed, bounded synthetic programs independently verify runtime output.
    namespace = {}
    exec(compile(source, 'synthetic.py', 'exec'), namespace)
    assert namespace['FORMAT'] == expected
    assert checker.scan_component('synthetic.py', expected, NAME_CONTRACTS)
    (tmp_path / 'synthetic.py').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'from pathlib import Path\nFORMAT = Path(LEGACY_STEM + "+unregistered-v99")',
    'def build(value: LEGACY_STEM + "-unregistered-v99"):\n pass',
    'def build() -> LEGACY_STEM + "-unregistered-v99":\n pass',
    'stem = "synthetic"\ntry:\n values = ((stem := LEGACY_STEM), operation(), (stem := "synthetic"))\nexcept Exception:\n pass\nFORMAT = f"{stem}-unregistered-v99"',
    'from os.path import join\nFORMAT = join(LEGACY_STEM, "unregistered-v99")',
    'from pathlib import Path\nFORMAT = Path(LEGACY_STEM, "unregistered-v99")',
])
def test_reviewed_annotation_exception_and_path_outputs(tmp_path, source):
    (tmp_path / 'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path)


def test_reviewed_json_escape_is_decoded(tmp_path):
    (tmp_path / 'synthetic.json').write_text(r'{"format":"\u0072ecordbench-unregistered-v99"}')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'value = None\nfor item in range(3):\n if value is None:\n  value = LEGACY_STEM\n else:\n  value += ".env"\nFORMAT = value',
    'stem = "synthetic"\nfor next_stem in [LEGACY_STEM, "synthetic"]:\n FORMAT = f"{stem}-unregistered-v99"\n stem = next_stem',
    'stem = "synthetic"\nvalue = (stem := LEGACY_STEM) if selected else (stem := "synthetic")\nFORMAT = f"{stem}-unregistered-v99"',
    'value = LEGACY_STEM\nwhile (value := value + ".env"):\n break',
    'settings = {"stem": LEGACY_STEM}\nfor _ in range(2):\n settings = dict(settings, stem=settings["stem"] + ".env")',
])
def test_loop_and_expression_policy_rejects_unmodeled_state(tmp_path, source):
    (tmp_path / 'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert any('unsupported legacy syntax' in x.identifier for x in checker.findings(tmp_path))


@pytest.mark.parametrize('source', [
    'for _ in range(2):\n stem = LEGACY_STEM\n FORMAT = stem + ".env"',
    'for _ in range(2):\n stem, other = LEGACY_STEM, "synthetic"\n FORMAT = stem + ".env"',
    'names = set()\nfor _ in range(2):\n names = names | {LEGACY_STEM}',
    'settings = {}\nfor _ in range(2):\n settings = dict(settings, stem=LEGACY_STEM)',
    'from os.path import join\nFORMAT = join("synthetic", "example")',
    'from pathlib import Path\nFORMAT = Path(LEGACY_STEM + ".env")',
])
def test_bounded_loop_resets_and_immutable_accumulation_remain_supported(tmp_path, source):
    (tmp_path / 'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source + '\n')
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize('contents', [r'{"format":"\uZZZZ"}', '{"format":', '{"format":"unterminated}'])
def test_undecodable_json_is_not_safe_unknown(tmp_path, contents):
    (tmp_path / 'synthetic.json').write_text(contents)
    assert any('undecodable JSON' in x.identifier for x in checker.findings(tmp_path))


@pytest.mark.parametrize('suffix', ['.py', '.js', '.cjs', '.mjs', '.json'])
def test_undecodable_operational_source_is_rejected(tmp_path, suffix):
    (tmp_path / ('synthetic' + suffix)).write_bytes(b'\xff')
    assert any('undecodable operational source' in x.identifier for x in checker.findings(tmp_path))


@pytest.mark.parametrize('escape', [r'\uZZZZ', r'\xQ1', r'\u{110000}'])
def test_undecodable_javascript_literals_are_rejected(tmp_path, escape):
    (tmp_path / 'synthetic.js').write_text('const text = "' + escape + '";\n')
    assert any('undecodable JavaScript' in x.identifier for x in checker.findings(tmp_path))


def test_json_escape_keys_and_single_decoding(tmp_path):
    path = tmp_path / 'synthetic.json'
    path.write_text(r'{"\u0072ecordbench-unregistered-v99":"synthetic"}')
    assert checker.findings(tmp_path)
    path.write_text(r'{"format":"\\u0072ecordbench-unregistered-v99"}')
    assert checker.findings(tmp_path) == []



def test_javascript_malformed_comment_does_not_disable_escape_inspection(tmp_path):
    source = r'const regex = /\//; const FORMAT = "\x72ecordbench-unregistered-v99"; // \uZZZZ'
    (tmp_path / 'synthetic.js').write_text(source)
    assert checker.findings(tmp_path)


def test_loop_accumulator_alias_does_not_hide_prior_iteration(tmp_path):
    source = 'from case_intelligence.legacy_names import LEGACY_STEM\nsettings = {"stem": LEGACY_STEM}\nfor _ in range(2):\n previous = settings\n settings = dict(settings, stem=previous["stem"] + ".env")\n'
    namespace = {}
    exec(compile(source, 'synthetic.py', 'exec'), namespace)
    assert namespace['settings']['stem'] == 'record' + 'bench.env.env'
    assert checker.scan_component('synthetic.py', namespace['settings']['stem'], NAME_CONTRACTS)
    (tmp_path / 'synthetic.py').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'settings = {"stem": LEGACY_STEM}\nfor _ in range(2):\n first = settings\n previous = first\n settings = dict(settings, stem=previous["stem"] + ".env")',
    'names = {LEGACY_STEM}\nfor _ in range(2):\n previous = names\n names = names | {item + ".env" for item in previous}',
])
def test_accumulator_inputs_written_in_loop_are_unsupported(tmp_path, source):
    (tmp_path / 'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source)
    assert any('unsupported legacy syntax' in item.identifier for item in checker.findings(tmp_path))


def test_javascript_decode_uncertainty_preserves_raw_findings():
    token = 'record' + 'bench-unregistered-v99'
    found = checker.scan_javascript('synthetic.js', 'const FORMAT = "' + token + r'"; // \uZZZZ', NAME_CONTRACTS)
    assert any(item.identifier == token for item in found)
    assert any('ambiguous JavaScript escape' in item.identifier for item in found)


@pytest.mark.parametrize('source', [
    'from pathlib import PurePath\nFORMAT = PurePath(LEGACY_STEM, "unregistered-v99")',
    'from pathlib import PosixPath\nFORMAT = PosixPath(LEGACY_STEM, "unregistered-v99")',
    'import pathlib\nFORMAT = pathlib.PureWindowsPath(LEGACY_STEM, "unregistered-v99")',
    'FORMAT = LEGACY_STEM + "@unregistered-v99"',
    'stem = "synthetic"\ntry:\n raise ValueError()\nexcept ValueError:\n stem = LEGACY_STEM\n operation()\n stem = "synthetic"\nfinally:\n FORMAT = f"{stem}-unregistered-v99"',
])
def test_latest_review_path_and_handler_boundaries(tmp_path, source):
    (tmp_path / 'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n' + source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('filename', ['compose.yaml', 'config.toml', '.env', 'Dockerfile', 'synthetic.sh', 'synthetic'])
def test_undecodable_configuration_and_extensionless_source_is_rejected(tmp_path, filename):
    (tmp_path / filename).write_bytes(b'key: recordbench-unregistered-v99\xff')
    assert checker.findings(tmp_path)


def test_javascript_quote_regex_and_malformed_comment_remains_blocked(tmp_path):
    (tmp_path / 'synthetic.js').write_text(r'// \uZZZZ' + '\n' + r'const regex = /"/; const FORMAT = "\x72ecordbench-unregistered-v99";')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('suite', [
    'except Exception:\n stem = LEGACY_STEM\n operation()\n stem = "synthetic"',
    'except Exception:\n stem = LEGACY_STEM\n unused = undefined_name\n stem = "synthetic"',
    'except Exception:\n pass\nelse:\n stem = LEGACY_STEM\n operation()\n stem = "synthetic"',
])
def test_exception_suite_unsupported_exits_are_not_modeled_safe(tmp_path, suite):
    source = 'from case_intelligence.legacy_names import LEGACY_STEM\nstem = "synthetic"\ntry:\n operation()\n' + suite + '\nfinally:\n FORMAT = f"{stem}-unregistered-v99"\n'
    (tmp_path / 'synthetic.py').write_text(source)
    assert any('exceptional handler or else' in item.identifier for item in checker.findings(tmp_path))



def test_unhandled_try_exit_reaches_finally_with_known_origin(tmp_path):
    source = 'from case_intelligence.legacy_names import LEGACY_STEM\ntry:\n stem = LEGACY_STEM\n operation()\n stem = "synthetic"\nfinally:\n FORMAT = f"{stem}-unregistered-v99"\n'
    (tmp_path / 'synthetic.py').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'other="synthetic"\ndel other\ntry:\n operation()\nexcept Exception:\n stem=LEGACY_STEM\n stem=other\nfinally:\n FORMAT=f"{stem}-unregistered-v99"',
    'import pathlib\nBuilder=pathlib.PurePath\nFORMAT=Builder(LEGACY_STEM,"unregistered-v99")',
    'def build(stem):\n return stem+"-unregistered-v99"\nFORMAT=build(LEGACY_STEM)',
    'build=lambda stem: stem+"-unregistered-v99"\nFORMAT=build(LEGACY_STEM)',
])
def test_reviewed_deletion_callable_alias_and_explicit_arguments(tmp_path, source):
    (tmp_path/'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n'+source)
    assert checker.findings(tmp_path)


def test_commonjs_legacy_octal_escape_is_not_safe_unknown(tmp_path):
    (tmp_path/'synthetic.cjs').write_text(r'const FORMAT="\162ecordbench-unregistered-v99";')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('target', ['recordbench-unregistered-v99','/synthetic/recordbench-unregistered-v99'])
def test_operational_symlink_target_names_are_checked_without_following(tmp_path, target):
    (tmp_path/'current').symlink_to(target)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('filename,source', [('compose.yaml',r'key: "\x72ecordbench-unregistered-v99"'),('config.toml',r'key = "\u0072ecordbench-unregistered-v99"')])
def test_escaped_configuration_identifiers_fail_closed(tmp_path, filename, source):
    (tmp_path/filename).write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'import pathlib\nFirst=pathlib.PurePath\nBuilder=First\nFORMAT=Builder(LEGACY_STEM,"unregistered-v99")',
    'def build(stem):\n return stem+"-unregistered-v99"\nAlias=build\nFORMAT=Alias(stem=LEGACY_STEM)',
    'FORMAT=(lambda stem: stem+"-unregistered-v99")(LEGACY_STEM)',
    'other="synthetic"\ndel (other,)\ntry:\n operation()\nexcept Exception:\n stem=LEGACY_STEM\n stem=other\nfinally:\n FORMAT=f"{stem}-unregistered-v99"',
])
def test_deleted_tuple_and_transitive_callables_remain_restricted(tmp_path, source):
    (tmp_path/'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n'+source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'from pathlib import Path\nBuilder=Path\nFORMAT=Builder(LEGACY_STEM+".env")',
    'def build(stem):\n return stem+"-example"\nFORMAT=build("synthetic")',
    'other="synthetic"\ndel other\nother="synthetic"\ntry:\n operation()\nexcept Exception:\n stem=LEGACY_STEM\n stem=other\nfinally:\n FORMAT=f"{stem}-unregistered-v99"',
])
def test_recognized_path_alias_and_fresh_nonlegacy_values_remain_valid(tmp_path, source):
    (tmp_path/'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n'+source)
    assert checker.findings(tmp_path)==[]


def test_symlink_target_contents_are_not_followed(tmp_path):
    destination=tmp_path/'tests'/'synthetic.txt'
    destination.parent.mkdir()
    destination.write_text('recordbench-unregistered-v99')
    (tmp_path/'current').symlink_to(destination)
    assert checker.findings(tmp_path)==[]



def test_registered_dangling_symlink_target_remains_valid(tmp_path):
    (tmp_path/'current').symlink_to('recordbench.env')
    assert checker.findings(tmp_path)==[]



def test_delete_target_expressions_still_scan_decoded_literals(tmp_path):
    (tmp_path/'synthetic.py').write_text(r'del mapping["\x72ecordbench-unregistered-v99"]')
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('tail', [
    'def use(callback=build):\n return callback(LEGACY_STEM)\nFORMAT=use()',
    'for callback in [build]:\n FORMAT=callback(LEGACY_STEM)',
])
def test_local_callable_kind_survives_captured_and_iterated_values(tmp_path, tail):
    source='from case_intelligence.legacy_names import LEGACY_STEM\ndef build(stem):\n return stem+"-unregistered-v99"\n'+tail
    (tmp_path/'synthetic.py').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('selection', [
    'Alias = build if selected else str',
    'Alias = str if selected else build',
    'if selected:\n Alias = build\nelse:\n Alias = str',
    'Alias = selected and build or str',
    'Alias = (build if selected else str) if other else str',
    'choices = [build] if selected else [str]\nAlias = choices[0]',
    'for Alias in [build, str]:\n pass',
])
def test_conditional_callable_aliases_retain_restricted_origin(tmp_path, selection):
    source = 'from case_intelligence.legacy_names import LEGACY_STEM\ndef build(stem):\n return stem+"-unregistered-v99"\n' + selection + '\nFORMAT = Alias(LEGACY_STEM)\n'
    (tmp_path/'synthetic.py').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('filename,source', [
    ('compose.yaml', 'key: "record\\\n  bench-unregistered-v99"\n'),
    ('config.toml', 'key = """record\\\n  bench-unregistered-v99"""\n'),
    ('compose.yaml', 'key: "record\\\r\n  bench-unregistered-v99"\r\n'),
    ('config.toml', 'key = """record\\  \n  bench-unregistered-v99"""\n'),
])
def test_configuration_continuations_fail_closed(tmp_path, filename, source):
    (tmp_path/filename).write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    r'const FORMAT = "\x72ecord" + "bench-unregistered-v99";',
    r'const first = "\u0072ecord"; const FORMAT = first + "bench-unregistered-v99";',
    r'const FORMAT = `\u{72}ecord${"bench-unregistered-v99"}`;',
    r'const FORMAT = "r\ecord" + "bench-unregistered-v99";',
    'const FORMAT = "rec\\\nord" + "bench-unregistered-v99";',
])
def test_escaped_javascript_fragments_cannot_compose_unchecked(tmp_path, source):
    (tmp_path/'synthetic.js').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('relative', ['synthetic.sh', 'compose.yaml'])
@pytest.mark.parametrize('placeholder', ['${myrecord' + 'bench_unregistered_v99}', '{myrecord' + 'bench_unregistered_v99}', '@MYRECORDBENCH_UNREGISTERED_V99@'])
def test_placeholder_identifiers_are_scanned_before_masking(tmp_path, relative, placeholder):
    (tmp_path/relative).write_text('\nVALUE=' + placeholder + '\n')
    found = checker.findings(tmp_path)
    assert any(item.location == relative + ':2' and item.identifier.casefold() == 'myrecord' + 'bench_unregistered_v99' for item in found)


@pytest.mark.parametrize('selection', [
    'Alias = build if selected else LEGACY_STEM',
    'if selected:\n Alias = build\nelse:\n Alias = unknown',
    'choices = [build, str]\nAlias = [item for item in choices if selected][0]',
    'Alias = build if selected else str\ndef use(callback=Alias):\n return callback(LEGACY_STEM)\nFORMAT=use()\nAlias = str',
])
def test_ambiguous_callable_choices_do_not_erase_identity(tmp_path, selection):
    source = 'from case_intelligence.legacy_names import LEGACY_STEM\ndef build(stem):\n return stem+"-unregistered-v99"\n' + selection + '\nFORMAT = Alias(LEGACY_STEM)\n'
    (tmp_path/'synthetic.py').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'Alias = str if selected else str\nFORMAT = Alias(LEGACY_STEM)',
    'def build(stem):\n return stem+"-example"\nAlias = build if selected else str\nFORMAT = Alias("synthetic")',
])
def test_supported_callable_choices_keep_exact_or_nonlegacy_behavior(tmp_path, source):
    (tmp_path/'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\n'+source)
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize('placeholder', ['${RECORDBENCH_TEST_SETTING}', '{RECORDBENCH_TEST_SETTING}', '@RECORDBENCH_TEST_SETTING@', '${SYNTHETIC_VALUE}'])
def test_registered_and_nonlegacy_placeholder_identifiers_remain_valid(placeholder):
    assert checker.scan_text('synthetic.sh', 'VALUE=' + placeholder) == []


def test_control_regex_escapes_do_not_create_identifier_fragments():
    source = r'const sanitize = value => value.replace(/[\u0000-\u0008\ud800-\udfff]/gu, " ");'
    assert checker.scan_javascript('synthetic.js', source, NAME_CONTRACTS) == []


@pytest.mark.parametrize('filename,source', [
    ('compose.yaml', 'run: |\n  echo \\\n    synthetic\n'),
    ('compose.yaml', 'key: "record\\\n  bench.env"\n'),
    ('config.toml', 'key = """record\\\n  bench.env"""\n'),
])
def test_configuration_continuation_safe_controls(tmp_path, filename, source):
    (tmp_path/filename).write_text(source)
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize('selection', [
    'Alias = [build, str][selected]',
    'Alias = {"first": build, "second": str}[selected]',
])
def test_dynamic_callable_member_selection_retains_restriction(tmp_path, selection):
    source = 'from case_intelligence.legacy_names import LEGACY_STEM\ndef build(stem):\n return stem+"-unregistered-v99"\n' + selection + '\nFORMAT=Alias(LEGACY_STEM)'
    (tmp_path/'synthetic.py').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('selection', [
    'choices={"yes":build,"no":str}\nAlias=choices.get(selected,str)',
    'choices={"yes":build}\nAlias=choices.get("yes",str)',
    'choices={}\nAlias=choices.get(selected,build)',
])
def test_dictionary_callable_selectors_preserve_identity(tmp_path, selection):
    source='from case_intelligence.legacy_names import LEGACY_STEM\ndef build(stem):\n return stem+"-unregistered-v99"\n'+selection+'\nFORMAT=Alias(LEGACY_STEM)'
    (tmp_path/'synthetic.py').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('line_break', ['\x85','\u2028','\u2029'])
def test_yaml_unicode_continuation_breaks_fail_closed(tmp_path, line_break):
    source='key: "record\\'+line_break+'  bench-unregistered-v99"\n'
    (tmp_path/'compose.yaml').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'const FORMAT = "record" + "bench-unregistered-v99";',
    'const FORMAT = ("record") + ("bench-unregistered-v99");',
    'const FORMAT = "record" /* synthetic */ + "bench-unregistered-v99";',
])
def test_plain_javascript_literal_composition_is_unsupported(tmp_path, source):
    (tmp_path/'synthetic.js').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'VALUE=rec${EMPTY}ordbench-unregistered-v99',
    'KNOWN=recordbench.env\nVALUE=rec${EMPTY}ordbench-unregistered-v99',
    'VALUE=r${FIRST}ec${SECOND}ordbench-unregistered-v99',
])
def test_empty_placeholders_cannot_hide_assembled_legacy_stem(tmp_path, source):
    (tmp_path/'synthetic.sh').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('selection', [
    'choices={"yes":build,"no":str}\nAlias=choices.get("no",build)',
    'choices={}\nAlias=choices.get("missing",str)',
])
def test_exact_dictionary_callable_selection_preserves_supported_choice(tmp_path, selection):
    source='from case_intelligence.legacy_names import LEGACY_STEM\ndef build(stem):\n return stem+"-unregistered-v99"\n'+selection+'\nFORMAT=Alias(LEGACY_STEM)'
    (tmp_path/'synthetic.py').write_text(source)
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize('source', [
    'VALUE=rec${EMPTY}ordbench.env',
    'VALUE=recordbench-backup-{NODE_ID}.service',
    'VALUE=synthetic${EMPTY}text',
])
def test_empty_placeholder_controls_preserve_registered_and_nonlegacy_names(source):
    assert checker.scan_text('synthetic.sh',source) == []


@pytest.mark.parametrize('selection', [
    'def select(callback):\n return callback\nAlias=select(build)',
    'choices={"yes":build}\nAlias=choices.pop("yes")',
    'Alias=build.__call__',
])
def test_unmodeled_callable_consumers_preserve_possible_identity(tmp_path, selection):
    source='from case_intelligence.legacy_names import LEGACY_STEM\ndef build(stem):\n return stem+"-unregistered-v99"\n'+selection+'\nFORMAT=Alias(LEGACY_STEM)'
    (tmp_path/'synthetic.py').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'choices={"yes":build,"no":str}\ngetter=choices.get\nAlias=getter("yes")\nFORMAT=Alias(LEGACY_STEM)',
    'import functools\nAlias=functools.partial(build,LEGACY_STEM)\nFORMAT=Alias()',
])
def test_bound_selectors_and_captured_callable_arguments_fail_closed(tmp_path, source):
    (tmp_path/'synthetic.py').write_text('from case_intelligence.legacy_names import LEGACY_STEM\ndef build(stem):\n return stem+"-unregistered-v99"\n'+source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'const re = /"/; const FORMAT = "record" + "bench-unregistered-v99";',
    'const re = /"/; const FORMAT = ("record") /* synthetic */ + ("bench-unregistered-v99");',
])
def test_javascript_regex_quotes_cannot_disable_composition_check(tmp_path, source):
    (tmp_path/'synthetic.js').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('source', [
    'EMPTY=; VALUE=rec$EMPTY"ordbench-unregistered-v99"',
    'EMPTY=; VALUE="rec"$EMPTY"ordbench-unregistered-v99"',
])
def test_unbraced_shell_parameters_cannot_hide_legacy_stem(tmp_path, source):
    (tmp_path/'synthetic.sh').write_text(source)
    assert checker.findings(tmp_path)


@pytest.mark.parametrize('depth', [4, 8, 16])
def test_ordinary_call_chains_have_bounded_analysis_work(depth):
    import sys
    count = 0
    previous = sys.getprofile()
    def profile(frame, event, argument):
        nonlocal count
        if event == 'call' and frame.f_code.co_filename == checker.__file__ and frame.f_code.co_name in {'frozen','structure','value','call_inputs','contains_stem'}:
            count += 1
            if count > 10000:
                raise RuntimeError('synthetic analysis work budget exceeded')
    try:
        sys.setprofile(profile)
        found = checker.scan_python('synthetic.py', 'def literal(value):\n return value' + '.replace("x", "y")' * depth + '\n', NAME_CONTRACTS)
    finally:
        sys.setprofile(previous)
    assert found == []
    assert count <= 10000


def test_analysis_budget_exhaustion_is_blocking(monkeypatch):
    monkeypatch.setattr(checker, 'MAX_ANALYSIS_STEPS', 100, raising=False)
    source = 'def literal(value):\n return value' + '.replace("x", "y")' * 16 + '\n'
    assert checker.scan_python('synthetic.py', source, NAME_CONTRACTS) == [
        checker.Finding('synthetic.py:1', 'unsupported Python analysis budget exhausted')
    ]


@pytest.mark.parametrize('source', [
    'const re = /"/; const FORMAT = "synthetic";',
    'const re = /[\x27\x22]/; const FORMAT = "synthetic";',
])
def test_quote_regex_without_string_composition_remains_valid(source):
    assert checker.scan_javascript('synthetic.js', source, NAME_CONTRACTS) == []


@pytest.mark.parametrize('source', [
    'EMPTY=; VALUE=rec$EMPTY"ordbench.env"',
    'EMPTY=; VALUE=rec$EMPTYordbench',
    'VALUE="$RECORDBENCH_TEST_SETTING"',
])
def test_shell_empty_parameter_controls_keep_lexical_boundaries(source):
    assert checker.scan_text('synthetic.sh', source) == []


@pytest.mark.parametrize('source', [
    'const left = "record"; const FORMAT = left + "bench-unregistered-v99";',
    'const right = "bench-unregistered-v99"; const FORMAT = "record" + right;',
    'const left = "record"; const FORMAT = (left) /* synthetic */ + ("bench-unregistered-v99");',
])
def test_javascript_variable_literal_composition_is_restricted(source):
    assert checker.scan_javascript('synthetic.js', source, NAME_CONTRACTS)


@pytest.mark.parametrize('operator', [':-', '-', ':+', '+', ':=', '='])
def test_shell_parameter_operators_cannot_hide_empty_boundary(operator):
    source = 'EMPTY=; VALUE=rec${EMPTY' + operator + '}"ordbench-unregistered-v99"'
    assert checker.scan_text('synthetic.sh', source)


@pytest.mark.parametrize('source', [
    'Alias=operator.methodcaller("format",LEGACY_STEM)\nFORMAT=Alias("{}-unregistered-v99")',
    'choices=[operator.methodcaller("format",LEGACY_STEM)]\nFORMAT=choices[0]("{}-unregistered-v99")',
    'FORMAT=operator.methodcaller("format",LEGACY_STEM)("{}-unregistered-v99")',
    'Alias=operator.methodcaller("format",LEGACY_STEM)\nFORMAT=Alias.__call__("{}-unregistered-v99")',
])
def test_invoking_unknown_legacy_values_is_restricted(source):
    source = 'import operator\nfrom case_intelligence.legacy_names import LEGACY_STEM\n' + source
    assert checker.scan_python('synthetic.py', source, NAME_CONTRACTS)


@pytest.mark.parametrize('depth', [8, 16, 22])
def test_unparse_node_work_is_bounded(monkeypatch, depth):
    import ast
    original = ast.unparse
    work = 0
    def counted(node):
        nonlocal work
        work += sum(1 for _ in ast.walk(node))
        if work > 10000:
            raise RuntimeError('synthetic unparse node-work budget exceeded')
        return original(node)
    monkeypatch.setattr(ast, 'unparse', counted)
    source = 'def literal(value):\n return value' + '.replace("x", "y")' * depth + '\n'
    assert checker.scan_python('synthetic.py', source, NAME_CONTRACTS) == []
    assert work <= 10000


def test_unparse_cost_is_reserved_before_running_it(monkeypatch):
    import ast
    monkeypatch.setattr(checker, 'MAX_ANALYSIS_STEPS', 1)
    def unexpected(node):
        raise AssertionError('unparse ran before its node cost was reserved')
    monkeypatch.setattr(ast, 'unparse', unexpected)
    assert checker.scan_python('synthetic.py', 'factory.method("synthetic")', NAME_CONTRACTS) == [
        checker.Finding('synthetic.py:1', 'unsupported Python analysis budget exhausted')
    ]


def test_large_unparse_fields_are_budgeted_before_rendering(monkeypatch):
    import ast
    original = ast.unparse
    monkeypatch.setattr(checker, 'MAX_ANALYSIS_STEPS', 300)
    def bounded(node):
        assert not any(isinstance(part, ast.Constant) and isinstance(part.value, str) and len(part.value) > 65536 for part in ast.walk(node))
        return original(node)
    monkeypatch.setattr(ast, 'unparse', bounded)
    source = 'factory("' + 'x' * 100000 + '").method()'
    assert checker.scan_python('synthetic.py', source, NAME_CONTRACTS) == [
        checker.Finding('synthetic.py:1', 'unsupported Python analysis budget exhausted')
    ]


@pytest.mark.parametrize('source', [
    'EMPTY=; VALUE=rec${EMPTY:-}"ordbench.env"',
    'EMPTY=; VALUE=${EMPTY:-recordbench-unregistered-v99}',
])
def test_shell_parameter_operator_contents_are_still_checked(source):
    assert bool(checker.scan_text('synthetic.sh', source)) == ('unregistered' in source)


@pytest.mark.parametrize('source', [
    'const badge = "99+";',
])
def test_javascript_literal_plus_data_remains_valid(source):
    assert checker.scan_javascript('synthetic.js', source, NAME_CONTRACTS) == []


@pytest.mark.parametrize('source', [
    'EMPTY=; VALUE=rec${EMPTY:-ord}bench-unregistered-v99',
    'EMPTY=; VALUE="rec"${EMPTY:-ord}"bench-unregistered-v99"',
    'EMPTY=; VALUE=${EMPTY:-rec}${EMPTY:-ordbench-unregistered-v99}',
])
def test_shell_operator_words_cannot_compose_unchecked_names(source):
    assert checker.scan_text('synthetic.sh', source)


@pytest.mark.parametrize('callee', [
    '(Alias := operator.methodcaller("format", LEGACY_STEM))',
    '(operator.methodcaller("format", LEGACY_STEM) if condition else other)',
    '(operator.methodcaller("format", LEGACY_STEM) or other)',
])
def test_composite_callable_expressions_preserve_unknown_legacy_origin(callee):
    source = 'import operator\nfrom case_intelligence.legacy_names import LEGACY_STEM\nFORMAT=' + callee + '("{}-unregistered-v99")'
    assert checker.scan_python('synthetic.py', source, NAME_CONTRACTS)


@pytest.mark.parametrize('source', [
    r"VALUE='{stem}\unregistered-v99'",
    r"VALUE='C:\{stem}\unregistered-v99'",
    r"VALUE='/synthetic/{stem}\unregistered-v99'",
])
def test_backslashes_remain_in_operational_tokens(source):
    source = source.replace('{stem}', LEGACY_STEM)
    assert checker.scan_text('synthetic.sh', source)


@pytest.mark.parametrize('source', [
    'EMPTY=; VALUE=rec${EMPTY:-ord}bench.env',
    'VALUE=${FIRST:-${SECOND:-synthetic}}',
])
def test_unsupported_shell_operator_families_reject_consistently(source):
    assert checker.scan_text('synthetic.sh', source)


@pytest.mark.parametrize('source', [
    'VALUE=${EMPTY:-recordbench.env}',
    r"VALUE='C:\synthetic\file.txt'",
])
def test_delimited_shell_defaults_and_nonlegacy_backslashes_are_valid(source):
    assert checker.scan_text('synthetic.sh', source) == []


def test_nonlegacy_assignment_expression_callee_remains_valid():
    assert checker.scan_python('synthetic.py', '(Alias := str)("synthetic")', NAME_CONTRACTS) == []


@pytest.mark.parametrize('base', ['AST', 'expr', 'stmt', 'Constant', 'Add'])
def test_unknown_python_ast_node_types_fail_closed_even_without_known_origins(base):
    import ast
    future_type = type('FutureSyntax', (getattr(ast, base),), {'_fields': ('payload',)})
    future = future_type()
    future.payload = ast.Constant(value='synthetic')
    future.value = 'synthetic'
    future.lineno = 1
    tree = ast.Module(body=[ast.Expr(value=future)], type_ignores=[])
    ast.fix_missing_locations(tree)
    found = checker.scan_python('synthetic.py', '', NAME_CONTRACTS, tree=tree)
    assert any('unsupported Python AST node (FutureSyntax)' in item.identifier for item in found)
    assert any('case_intelligence.legacy_names.name_value' in item.identifier for item in found)


def test_unknown_python_ast_node_preserves_decoded_literal_findings():
    import ast
    class FutureSyntax(ast.expr):
        _fields = ('payload',)
    future = FutureSyntax()
    future.payload = ast.Constant(value=LEGACY_STEM + '-unregistered-v99')
    tree = ast.fix_missing_locations(ast.Module(body=[ast.Expr(value=future)], type_ignores=[]))
    found = checker.scan_python('synthetic.py', '', NAME_CONTRACTS, tree=tree)
    assert any('unsupported Python AST node' in item.identifier for item in found)
    assert any(item.identifier == LEGACY_STEM + '-unregistered-v99' for item in found)


@pytest.mark.parametrize('expression', ['left + right', '`${left}${right}`', '[left, right].join("")'])
def test_javascript_unknown_composition_families_fail_closed(expression):
    source = 'const left="record"; const right="bench-unregistered-v99"; const FORMAT=' + expression + ';'
    assert checker.scan_javascript('synthetic.js', source, NAME_CONTRACTS)


def test_unknown_python_ast_diagnostic_cannot_receive_source_receipt(tmp_path, monkeypatch):
    import json
    manifest = tmp_path / 'forms.json'
    manifest.write_text(json.dumps({'version': 1, 'files': {'synthetic.py': {
        'source_sha256': 'a' * 64,
        'diagnostics': [{'line': 1,
                         'identifier': 'unsupported Python AST node (FutureSyntax); use case_intelligence.legacy_names.name_value',
                         'reason': 'synthetic', 'validation': 'synthetic'}]}}}))
    monkeypatch.setattr(checker, 'SOURCE_FORM_CONTRACT_PATH', manifest)
    with pytest.raises(ValueError, match='diagnostic contract'):
        checker.source_form_contracts()


def test_unsupported_syntax_cli_names_rename_compat_helper(monkeypatch, capsys):
    monkeypatch.setattr(checker, 'findings', lambda: [checker.Finding('synthetic.sh:1', 'unsupported shell operator-word composition')])
    assert checker.main() == 1
    assert 'case_intelligence.legacy_names.name_value' in capsys.readouterr().err


@pytest.mark.parametrize('kind', ['addition', 'call'])
def test_unknown_python_ast_child_is_checked_before_value_resolution(kind):
    import ast
    class FutureSyntax(ast.expr):
        _fields = ()
    child = FutureSyntax()
    expression = (ast.BinOp(left=ast.Constant(value='synthetic'), op=ast.Add(), right=child)
                  if kind == 'addition' else
                  ast.Call(func=ast.Name(id='str', ctx=ast.Load()), args=[child], keywords=[]))
    tree = ast.fix_missing_locations(ast.Module(body=[ast.Expr(value=expression)], type_ignores=[]))
    found = checker.scan_python('synthetic.py', '', NAME_CONTRACTS, tree=tree)
    assert any('unsupported Python AST node (FutureSyntax)' in item.identifier for item in found)


def test_unknown_python_ast_preflight_obeys_existing_budget(monkeypatch):
    import ast
    monkeypatch.setattr(checker, 'MAX_ANALYSIS_STEPS', 2)
    tree = ast.parse('value = "synthetic"')
    assert checker.scan_python('synthetic.py', '', NAME_CONTRACTS, tree=tree) == [
        checker.Finding('synthetic.py:1', 'unsupported Python analysis budget exhausted')]


def test_javascript_route_template_requires_explicit_source_contract():
    source = 'const path = `${origin}/auth/login`;'
    assert any('unsupported JavaScript construction (interpolated template)' in item.identifier
               for item in checker.scan_javascript('synthetic.js', source, NAME_CONTRACTS))


@pytest.mark.parametrize('source', [
    'type Alias = LEGACY_STEM\nFORMAT=Alias.__value__ + "-unregistered-v99"',
    'type Alias = "record"\nFORMAT=Alias.__value__ + "bench-unregistered-v99"',
    'def helper[T: LEGACY_STEM](): pass',
    'class Helper[T: LEGACY_STEM]: pass',
])
def test_unmodeled_type_alias_and_type_parameter_syntax_is_rejected(source):
    source = 'from case_intelligence.legacy_names import LEGACY_STEM\n' + source
    found = checker.scan_python('synthetic.py', source, NAME_CONTRACTS)
    assert any('unsupported Python AST node' in item.identifier for item in found)


@pytest.mark.parametrize('source', ['def broken(:\n pass', 'value = "unterminated', '\x00'])
@pytest.mark.parametrize('relative', ['synthetic.py', 'synthetic', 'synthetic-tool'])
def test_python_parse_uncertainty_is_blocking(tmp_path, source, relative):
    (tmp_path / relative).write_text(source)
    found = checker.findings(tmp_path)
    assert any('unsupported Python parse' in item.identifier for item in found)
    assert any('case_intelligence.legacy_names.name_value' in item.identifier for item in found)
    assert any('unsupported Python parse' in item.identifier
               for item in checker.scan_python(relative, source, NAME_CONTRACTS))


def test_python_authority_parse_failure_is_not_replaced_by_a_clean_empty_tree(tmp_path):
    path = tmp_path / 'src/case_intelligence/legacy_names.py'
    path.parent.mkdir(parents=True)
    path.write_text('NAME_CONTRACTS = {\n')
    assert any('unsupported Python parse' in item.identifier for item in checker.findings(tmp_path))


def test_python_parse_failure_retains_raw_identifier_findings(tmp_path):
    identifier = LEGACY_STEM + '-unregistered-v99'
    (tmp_path / 'synthetic.py').write_text('def broken(:\n# ' + identifier)
    found = checker.findings(tmp_path)
    assert any('unsupported Python parse' in item.identifier for item in found)
    assert any(item.identifier == identifier for item in found)


@pytest.mark.parametrize('error_type', [SyntaxError, ValueError, RecursionError])
def test_python_parser_rejection_fails_closed_without_exception_text(monkeypatch, error_type):
    def fail(*args, **kwargs):
        raise error_type('synthetic parser detail')
    rejected = None
    with monkeypatch.context() as patch:
        patch.setattr(checker.ast, 'parse', fail)
        try:
            found = checker.scan_python('synthetic.py', 'synthetic', NAME_CONTRACTS)
        except error_type as error:
            rejected = error
    assert rejected is None
    assert any('unsupported Python parse' in item.identifier for item in found)
    assert all('synthetic parser detail' not in item.identifier for item in found)


@pytest.mark.parametrize('source', [
    r"VALUE=$'\x72ecordbench-unregistered-v99'",
    r"VALUE=$'\162ecordbench-unregistered-v99'",
    'VALUE=rec\\\nordbench-unregistered-v99',
    'VALUE="rec\\\nordbench-unregistered-v99"',
])
def test_shell_encoded_and_continued_names_are_blocked_with_bash_runtime_proof(tmp_path, source):
    import shutil
    import subprocess
    bash = shutil.which('bash')
    if bash is None:
        pytest.skip('Bash is required for the synthetic shell behavior proof')
    actual = subprocess.run([bash, '--noprofile', '--norc', '-c', source + '\nprintf "%s" "$VALUE"'],
                            capture_output=True, text=True, timeout=5, check=True)
    assert actual.stdout == LEGACY_STEM + '-unregistered-v99'
    (tmp_path / 'synthetic.sh').write_text(source)
    assert checker.findings(tmp_path)
    assert checker.scan_text('synthetic.sh', source)


@pytest.mark.parametrize('source', [r"VALUE=$'\u0072ecordbench-unregistered-v99'",
                                  r"VALUE=$'\U00000072ecordbench-unregistered-v99'",
                                  "VALUE=$'synthetic'", "VALUE=$'unfinished"])
def test_ansi_c_quoting_is_a_consistently_unsupported_shell_family(source):
    assert any('unsupported shell ANSI-C quoting' in item.identifier
               for item in checker.scan_text('synthetic.sh', source))


@pytest.mark.parametrize('relative,source', [
    ('Dockerfile', 'FROM synthetic\nRUN echo synthetic'),
    ('Makefile', 'check:\n\t@echo synthetic'),
    ('.gitignore', '*.synthetic\n'),
    ('install', '#!/bin/sh\necho synthetic'),
])
def test_known_non_python_extensionless_sources_keep_literal_checks(tmp_path, relative, source):
    path = tmp_path / relative
    path.write_text(source)
    assert checker.findings(tmp_path) == []
    path.write_text(source + '\n# ' + LEGACY_STEM + '-unregistered-v99\n')
    assert any(item.identifier == LEGACY_STEM + '-unregistered-v99' for item in checker.findings(tmp_path))


@pytest.mark.parametrize('relative', ['Dockerfile', 'Makefile', 'install'])
def test_valid_python_in_extensionless_source_still_gets_semantic_checks(tmp_path, relative):
    (tmp_path / relative).write_text('VALUE = "record" + "bench-unregistered-v99"')
    assert any(item.identifier == LEGACY_STEM + '-unregistered-v99' for item in checker.findings(tmp_path))


def test_python_shebang_remains_binding_for_known_other_filename(tmp_path):
    (tmp_path / 'Dockerfile').write_text('#!/usr/bin/env python3\ndef broken(:\n pass')
    assert any('unsupported Python parse' in item.identifier for item in checker.findings(tmp_path))


@pytest.mark.parametrize('source', ['VALUE=synthetic\\\n-value', 'VALUE="record\\\nbench.env"'])
def test_shell_continuation_controls_keep_registered_names_and_plain_text(source):
    assert checker.scan_text('synthetic.sh', source) == []


@pytest.mark.parametrize('diagnostic', ['unsupported Python parse; use case_intelligence.legacy_names.name_value',
                                      'unsupported shell ANSI-C quoting'])
def test_parse_and_shell_rejections_cannot_receive_source_receipts(tmp_path, monkeypatch, diagnostic):
    import json
    manifest = tmp_path / 'forms.json'
    manifest.write_text(json.dumps({'version': 1, 'files': {'synthetic.py': {
        'source_sha256': 'a' * 64,
        'diagnostics': [{'line': 1, 'identifier': diagnostic, 'reason': 'synthetic', 'validation': 'synthetic'}]}}}))
    monkeypatch.setattr(checker, 'SOURCE_FORM_CONTRACT_PATH', manifest)
    with pytest.raises(ValueError, match='diagnostic contract'):
        checker.source_form_contracts()
