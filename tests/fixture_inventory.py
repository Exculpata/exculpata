"""Check tracked fixture support sources without executing their import graphs."""
import hashlib
from pathlib import Path
import subprocess

from tests.fixture_write_policy import Violation, check_fixture_writes


# Each pin covers a reviewed fixed-input runtime harness, not a blanket file
# exemption: SQL, ownership and all other structural checks still apply.
REVIEWED_DYNAMIC_PROBES = {
    'tests/fixture_probe.py': '351e19742e54024b04505a3b856bcffc4ad055eb6b8aaa4618ea75ddf58fd1c7',
    'tests/runtime_probes.py': 'b00e4089f01a7646b2e6db0bffa64f99ab99660cd93991c2a94e4bf1246a6111',
}
DYNAMIC_RULE = 'dynamic execution is unsupported; use fixed-input reviewed probes or fixture_write()'


def check_inventory_source(source, filename):
    violations = check_fixture_writes(source, filename)
    pinned = REVIEWED_DYNAMIC_PROBES.get(filename)
    if pinned is not None:
        if hashlib.sha256(source.encode()).hexdigest() != pinned:
            violations.append(Violation(filename, 1,
                'reviewed runtime probe changed; review fixed inputs and update its fixture inventory pin', True))
        else:
            violations = [item for item in violations if item.rule != DYNAMIC_RULE]
    return violations


def inventory_paths(root):
    """Tracked test, script, benchmark and compatibility support share the boundary.

    Git's inventory avoids temporary editor/sync copies and does not infer Python
    imports or execute code. Newly added sources in these roots join when staged.
    """
    names = subprocess.check_output(
        ['git', 'ls-files', '-z', '--', '*.py'], cwd=root
    ).decode().split('\0')
    paths = [Path(name) for name in names if name]
    return [path for path in paths if path.as_posix() != 'tests/fixture_write.py'
            and ('tests' in path.parts[:-1] or path.parts[0] in {'scripts', 'benchmarks'}
                 or path.parts[:3] == ('services', 'transcription', 'compatibility'))]


def check_repository_fixtures(root):
    return [violation for path in inventory_paths(root)
            for violation in check_inventory_source((root / path).read_text(encoding='utf-8'), path.as_posix())]
