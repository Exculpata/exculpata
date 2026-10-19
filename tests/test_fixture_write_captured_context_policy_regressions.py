"""Captured ownership and explicit context operations retain fixture boundaries."""

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize("definition, invocation", [
    ("def expose():\n    yield shared", "leaked = next(expose())\nleaked.close()"),
    ("async def expose():\n    return shared", "pending = expose()"),
    ("def expose():\n    return expose() if retry else shared", "leaked = expose()"),
    ("def expose(flag):\n    yield shared", "leaked = next(expose(True))"),
])
@pytest.mark.parametrize("ordering", ["lexical", "late", "closure"])
def test_captured_shared_results_reject_unsupported_shapes(definition, invocation, ordering):
    binding = "shared = bench.workspace.connection\n"
    if ordering == "lexical":
        source = binding + definition + "\n" + invocation
    elif ordering == "late":
        source = definition + "\n" + binding + invocation
    else:
        body = definition + "\n" + binding + invocation
        source = "def outer():\n" + "\n".join("    " + line for line in body.splitlines()) + "\nouter()"
    violations = check_fixture_writes(source)
    assert any("unsupported" in item.rule for item in violations), violations


@pytest.mark.parametrize("source", [
    "async def expose():\n    return bench.workspace.connection\npending = expose()",
    "shared = bench.workspace.append_audit_event\ndef expose():\n    yield shared\noperations = expose()",
    "shared = bench.workspace.connection\nasync def expose():\n    global shared\n    return shared\npending = expose()",
    "def outer():\n    shared = bench.workspace.connection\n    async def expose():\n        nonlocal shared\n        return shared\n    pending = expose()\nouter()",
])
def test_captured_attributes_bound_workspace_methods_and_declared_cells_are_shared(source):
    assert any("unsupported" in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize("source", [
    "import sqlite3\nshared = sqlite3.connect(':memory:')\ndef expose():\n    yield shared\nresult = expose()",
    "shared = bench.workspace.connection\ndef expose():\n    yield resource\nresult = expose()",
    "shared = bench.workspace.connection\ndef expose(shared):\n    yield shared\nresult = expose(resource)",
    "import sqlite3\nshared = bench.workspace.connection\ndef expose():\n    shared = sqlite3.connect(':memory:')\n    yield shared\nresult = expose()",
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nshared = bench.workspace.connection\nasync def expose(shared=independent):\n    return shared\npending = expose()",
    "shared = bench.workspace.connection\ndef expose():\n    yield [shared for shared in resources]\nresult = expose()",
    "shared = bench.workspace.connection\ndef expose():\n    import resource as shared\n    yield shared\nresult = expose()",
    "shared = bench.workspace.connection\nasync def expose():\n    def shared():\n        return resource\n    return shared()\npending = expose()",
])
def test_capture_rule_preserves_independent_unrelated_and_shadowed_values(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize("method, arguments", [("__enter__", ""), ("__exit__", "None, None, None"), ("__exit__", "ValueError, failure, None")])
@pytest.mark.parametrize("form", ["direct", "saved", "getattr", "unbound", "saved_unbound"])
@pytest.mark.parametrize("scope", ["unlocked", "owner_lock", "fixture"])
def test_raw_connection_context_protocol_requires_yielded_handle(method, arguments, form, scope):
    if form == "direct":
        action = f"bench.workspace.connection.{method}({arguments})"
    elif form == "saved":
        action = f"operation = bench.workspace.connection.{method}\noperation({arguments})"
    elif form == "getattr":
        action = f"getattr(bench.workspace.connection, '{method}')({arguments})"
    elif form == "unbound":
        action = f"sqlite3.Connection.{method}(bench.workspace.connection{', ' if arguments else ''}{arguments})"
    else:
        action = f"operation = sqlite3.Connection.{method}\noperation(bench.workspace.connection{', ' if arguments else ''}{arguments})"
    if scope != "unlocked":
        context = "bench.workspace._lock" if scope == "owner_lock" else "fixture_write(bench.workspace)"
        action = f"with {context}:\n" + "\n".join("    " + line for line in action.splitlines())
    violations = check_fixture_writes("import sqlite3\n" + action)
    assert any("context" in item.rule or "transaction" in item.rule for item in violations), violations


@pytest.mark.parametrize("source", [
    "resource.__enter__()\nresource.__exit__(None, None, None)",
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nindependent.__enter__()\nindependent.__exit__(None, None, None)",
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nsqlite3.Connection.__enter__(independent)\nsqlite3.Connection.__exit__(independent, None, None, None)",
    "with fixture_write(bench.workspace) as handle:\n    handle.__enter__()\n    handle.__exit__(None, None, None)",
])
def test_explicit_context_protocol_preserves_nonshared_and_scoped_controls(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize("method", ["append_audit_event", "_append_audit_event_locked", "_timestamp", "arbitrary_future_method"])
@pytest.mark.parametrize("form", ["direct", "saved", "getattr", "helper", "returned"])
def test_known_workspace_methods_cannot_end_sql_fixture_transactions(method, form):
    prefix = ""
    if form == "direct":
        action = f"bench.workspace.{method}()"
    elif form == "saved":
        prefix = f"operation = bench.workspace.{method}\n"
        action = "operation()"
    elif form == "getattr":
        action = f"getattr(bench.workspace, '{method}')()"
    elif form == "helper":
        prefix = f"def invoke(store):\n    store.{method}()\n"
        action = "invoke(bench.workspace)"
    else:
        prefix = f"def operation(store):\n    return store.{method}\n"
        action = "operation(bench.workspace)()"
    violations = check_fixture_writes(prefix + "with fixture_write(bench.workspace):\n    " + action)
    assert any("workspace method" in item.rule for item in violations), violations


@pytest.mark.parametrize("source", [
    "bench.workspace.append_audit_event()",
    "with bench.workspace._lock:\n    bench.workspace.append_audit_event()",
    "with fixture_write(bench.workspace, media=True):\n    bench.workspace.append_audit_event()",
    "with fixture_write(bench.workspace):\n    other.workspace.append_audit_event()",
    "with fixture_write(bench.workspace):\n    resource.append_audit_event()",
    "def operation(store):\n    return store.append_audit_event\nwith fixture_write(bench.workspace):\n    operation(other.workspace)()",
])
def test_workspace_method_rule_preserves_outside_scope_other_owner_and_media_mode(source):
    assert check_fixture_writes(source) == []


def test_nested_sql_scope_does_not_inherit_media_method_permission():
    source = '''with fixture_write(bench.workspace, media=True):
    with fixture_write(bench.workspace):
        bench.workspace.append_audit_event()
'''
    assert any("workspace method" in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize("class_name", ["WorkspaceStore", "Store", "workspace_store.WorkspaceStore"])
@pytest.mark.parametrize("form", ["direct", "saved", "getattr"])
def test_unbound_workspace_methods_retain_explicit_owner(class_name, form):
    imports = "from case_intelligence.workspace_store import WorkspaceStore, WorkspaceStore as Store\nfrom case_intelligence import workspace_store\n"
    if form == "direct":
        action = f"{class_name}.append_audit_event(bench.workspace)"
    elif form == "saved":
        action = f"operation = {class_name}.append_audit_event\noperation(bench.workspace)"
    else:
        action = f"getattr({class_name}, 'append_audit_event')(bench.workspace)"
    source = imports + "with fixture_write(bench.workspace):\n" + "\n".join("    " + line for line in action.splitlines())
    assert any("workspace method" in item.rule for item in check_fixture_writes(source))


def test_unbound_workspace_method_preserves_other_owner_control():
    assert check_fixture_writes('''from case_intelligence.workspace_store import WorkspaceStore
with fixture_write(bench.workspace):
    WorkspaceStore.append_audit_event(other.workspace)
''') == []


def test_unpacked_fixture_owner_does_not_claim_unrelated_unknown_mapping():
    assert check_fixture_writes('''store, matter = fixture
with fixture_write(store):
    value = {'synthetic': 'value'}.get(key)
    other.operation()
''') == []


def test_unpacked_fixture_argument_still_establishes_workspace_ownership():
    violations = check_fixture_writes('''store, matter = fixture
with fixture_write(store):
    store.append_audit_event()
''')
    assert any("workspace method" in item.rule for item in violations)
