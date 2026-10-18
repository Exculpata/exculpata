"""New legacy identifiers require an explicit compatibility contract."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from string import Formatter

import pytest

from case_intelligence.legacy_names import NAME_CONTRACTS, NameContract

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
    assert checker.findings(tmp_path) == [checker.Finding(f"scripts/{filename}:1", filename)]


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
    assert checker.findings(tmp_path) == []


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
    'value = reader(LEGACY_STEM)\nFORMAT = f"{value}-synthetic"',
    'parser = Parser(prog=LEGACY_STEM)\nFORMAT = f"{parser.read()}-synthetic"',
    'value = text.removeprefix(LEGACY_STEM)\nFORMAT = f"{value}-synthetic"',
    'value = text.replace(LEGACY_STEM, "synthetic")\nFORMAT = f"{value}-synthetic"',
    'value = mapping.get(LEGACY_STEM)\nFORMAT = f"{value}-synthetic"',
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
    assert checker.findings(tmp_path) == []


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
    'settings.stem = LEGACY_STEM\nFORMAT = f"{settings.stem}-exact-v1"',
    'settings.stem = LEGACY_STEM\nsettings.stem = "synthetic"\nFORMAT = f"{settings.stem}-unregistered-v99"',
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
    'settings.stem = "synthetic"\nclass Builder:\n settings = SimpleNamespace()\n settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n import synthetic as settings\n settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = "synthetic"\nclass Builder:\n from synthetic import settings\n settings.stem = LEGACY_STEM\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
    'settings.stem = LEGACY_STEM\nclass Builder:\n settings.stem = "synthetic"\n def build(self):\n  return f"{settings.stem}-unregistered-v99"',
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
