"""Keep automatic parser updates within the supported compatibility boundary."""
from pathlib import Path
import tomllib

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_dev_parser_uses_the_supported_tree_sitter_version():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    dependencies = project['project']['optional-dependencies']['dev']
    parser_dependencies = [item for item in dependencies if item.startswith('tree-sitter==')]
    assert parser_dependencies == ['tree-sitter==0.25.2']


def test_dependabot_keeps_parser_minor_and_major_updates_manual():
    config = yaml.safe_load((ROOT / '.github/dependabot.yml').read_text())
    updates = [item for item in config['updates']
               if item['package-ecosystem'] == 'pip' and '/' in item['directories']]
    assert len(updates) == 1
    ignores = [item for item in updates[0]['ignore']
               if item['dependency-name'] == 'tree-sitter']
    assert ignores == [{'dependency-name': 'tree-sitter', 'update-types': [
        'version-update:semver-minor', 'version-update:semver-major']}]
