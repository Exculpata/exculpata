def test_package_imports() -> None:
    import case_intelligence

    assert case_intelligence.__version__ == "0.1.0b1"


def test_application_transcription_and_installer_versions_agree():
    import ast
    from pathlib import Path
    import tomllib

    root = Path(__file__).parents[1]
    def constant(path, name):
        tree = ast.parse(path.read_text())
        return next(ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == name for t in node.targets))
    version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    assert tomllib.loads((root / 'services/transcription/pyproject.toml').read_text())['project']['version'] == version
    assert constant(root / 'src/case_intelligence/__init__.py', '__version__') == version
    assert constant(root / 'services/transcription/src/transcription_v2/__init__.py', '__version__') == version
    assert constant(root / 'scripts/exculpata_install.py', 'VERSION').replace('-beta.', 'b') == version
