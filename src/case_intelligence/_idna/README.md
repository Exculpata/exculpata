# Offline IDNA validation

The installer, application settings and relay share IDNA2008 validation without
requiring a host package installation or network request during preflight.
`idna-3.20.pyz` contains unchanged pure-Python source files from the existing
hash-locked idna 3.20 wheel plus its BSD-3-Clause license. It is a source archive,
not an executable or a model payload. The loader checks its SHA-256 before import
and uses a private package namespace. HTTPX retains its separately locked idna
3.20 dependency; agreement tests cover the two paths.

`manifest.json` records the upstream wheel hash and every included source hash.
The upstream license is retained here and inside the archive. Wheel metadata and
unused CLI modules are omitted; no included source is edited. To reproduce:

```sh
python -m pip download --only-binary=:all: --no-deps --require-hashes \
  -r deploy/egress-proxy/requirements.lock -d /tmp/idna-source
python scripts/vendor-idna.py /tmp/idna-source/idna-3.20-py3-none-any.whl \
  src/case_intelligence/_idna
```

Review the resulting archive hash before updating the loader's exact hash. Never
accept an arbitrary archive or relax the integrity check. Publication inspection
also scans archive members. First-party source-size and network gates are unchanged;
this pinned upstream dependency is distinct from first-party operational code.
