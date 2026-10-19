"""Pin the reviewed disposition and exercise the real scanner with synthetic data."""

from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = (
    'e5efe2ab089c892585853297b6793e0b042a9347:'
    'tests/runtime_probes.py:generic-api-key:13'
)


class HistoricalChecksumExceptionTests(unittest.TestCase):
    def test_only_the_reviewed_historical_fingerprint_is_ignored(self):
        lines = (ROOT / '.gitleaksignore').read_text().splitlines()
        entries = [line.strip() for line in lines
                   if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(entries, [FINGERPRINT])

    def test_disposition_has_issue_and_removal_condition(self):
        text = (ROOT / '.gitleaksignore').read_text()
        self.assertIn('https://github.com/Exculpata/exculpata/issues/340', text)
        self.assertIn('Remove when this historical finding is no longer reachable '
                      'by the all-branch scan.', text)

    @unittest.skipUnless(os.environ.get('GITLEAKS'),
                         'Real-scanner controls run in the secret-scan job')
    def test_other_commits_paths_and_synthetic_tokens_remain_detectable(self):
        scanner = os.environ['GITLEAKS']
        version = subprocess.check_output([scanner, 'version'], text=True).strip()
        self.assertEqual(version, '8.30.1')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(
                    ['git', '-C', str(root), *args], text=True,
                    stderr=subprocess.PIPE).strip()

            git('init', '-q')
            git('config', 'user.name', 'Synthetic Contributor')
            git('config', 'user.email', 'synthetic@example.invalid')
            (root / '.gitleaksignore').write_bytes((ROOT / '.gitleaksignore').read_bytes())
            (root / 'tests').mkdir()
            public_checksum = 'adb0fbfd70a3e2cade142c7da019ab7806f76c134990952c54ffb06ce12ac65c'
            line = '_AUTH_MODE_PROBE_SHA256 = ' + repr(public_checksum) + '\n'
            (root / 'tests/runtime_probes.py').write_text('\n' * 12 + line)
            (root / 'another_path.py').write_text(line)
            token = 'ghp_' + 'Ab3dE5fG7hJ9kL2mN4pQ6rS8tU1vW3xY5zA7'
            (root / 'synthetic.py').write_text('github_token = ' + repr(token) + '\n')
            git('add', '.')
            git('commit', '-qm', 'Synthetic scanner controls')
            commit = git('rev-parse', 'HEAD')
            report = root / 'report.json'
            result = subprocess.run(
                [scanner, 'git', '--redact', '--no-banner', '--log-opts=--all',
                 '--report-format=json', '--report-path', str(report)],
                cwd=root, capture_output=True, check=False)
            self.assertEqual(result.returncode, 1)
            findings = json.loads(report.read_text())
            fingerprints = {item['Fingerprint'] for item in findings}
            for path, number, rule in (
                ('tests/runtime_probes.py', 13, 'generic-api-key'),
                ('another_path.py', 1, 'generic-api-key'),
                ('synthetic.py', 1, 'github-pat'),
            ):
                self.assertIn(f'{commit}:{path}:{rule}:{number}', fingerprints)
            self.assertNotIn(FINGERPRINT, fingerprints)


if __name__ == '__main__':
    if not os.environ.get('GITLEAKS'):
        raise SystemExit('Set GITLEAKS to the checksum-verified 8.30.1 binary.')
    unittest.main()
