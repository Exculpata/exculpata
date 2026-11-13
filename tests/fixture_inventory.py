"""Check test fixture sources without executing their import graphs."""
import hashlib
from pathlib import Path

from tests.fixture_write_policy import Violation, check_fixture_writes


# Each pin covers a reviewed fixed-input runtime harness, not a blanket file
# exemption: SQL, ownership and all other structural checks still apply.
REVIEWED_DYNAMIC_PROBES = {
    'tests/fixture_probe.py': '351e19742e54024b04505a3b856bcffc4ad055eb6b8aaa4618ea75ddf58fd1c7',
    'tests/runtime_probes.py': '26bc85282c1953dbe16f2f7fc809063ec1bb700fd6197ffc3247b9fa369d57bc',
    'tests/test_sandbox_probe.py': '295015f538780aad251aa574f27687d59200f9da929e0e63148615de4f74dfa7',
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
    """Match main's tests/ scope, including sources that are not yet staged."""
    root = Path(root)
    return [path.relative_to(root) for path in sorted((root / 'tests').rglob('*.py'))
            if path != root / 'tests/fixture_write.py']


def check_repository_fixtures(root):
    return [violation for path in inventory_paths(root)
            for violation in check_inventory_source((root / path).read_text(encoding='utf-8'), path.as_posix())]
