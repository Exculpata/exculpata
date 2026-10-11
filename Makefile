PYTHON ?= .venv/bin/python
SYSTEM_PYTHON ?= python3.12
PUBLICATION_GUARD_ROOT ?= $(HOME)/.local/share/recordbench-publication-guard
GITLEAKS ?= gitleaks
.DEFAULT_GOAL := test

.PHONY: bootstrap hooks check check-fast compile compose-check publication-check publication-candidate-check secret-check deployment-check test test-fast test-transcription test-postgres

bootstrap:
	"$(SYSTEM_PYTHON)" scripts/bootstrap-dev.py
	$(MAKE) hooks

# Reuse the installed external guard without replacing reviewed code or settings.
# New installations pin an external interpreter and scanner, never branch-owned hooks.
hooks:
	@set -eu; \
	root="$$(git rev-parse --show-toplevel)"; \
	hooks="$$(git config --path --get core.hooksPath || true)"; \
	if [ -n "$$hooks" ]; then \
		test -x "$$hooks/pre-push"; \
		test -f "$$hooks/pre-push-publication.py"; \
		test -f "$$hooks/publication-check.py"; \
		grep -F 'pre-push-publication.py' "$$hooks/pre-push" > /dev/null; \
		resolved="$$(cd "$$hooks" && pwd -P)"; \
		case "$$resolved" in "$$root"|"$$root"/*) echo 'Publication hooks must be outside the checkout.' >&2; exit 1;; esac; \
		echo 'Preserving installed external pre-push guard.'; \
	else \
		mkdir -p "$(PUBLICATION_GUARD_ROOT)"; \
		guard="$$(mktemp -d "$(PUBLICATION_GUARD_ROOT)/reviewed-XXXXXXXX")"; \
		"$(SYSTEM_PYTHON)" -m venv "$$guard/python"; \
		"$$guard/python/bin/python" -m pip install --disable-pip-version-check 'pypdf>=6,<7'; \
		"$$guard/python/bin/python" scripts/install-publication-hook.py "$$guard/hooks"; \
	fi

test:
	"$(PYTHON)" -m pytest -q

test-fast:
	CASE_REVIEW_POSTGRES_TEST_DSN= CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0 \
		OMP_THREAD_LIMIT=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
		"$(PYTHON)" -m pytest -n auto --dist loadgroup --max-worker-restart=0 -q

test-transcription:
	PYTHONPATH=services/transcription/src "$(PYTHON)" -c 'import os, pytest; os.chdir("services/transcription"); raise SystemExit(pytest.main(["-q"]))'

compile:
	"$(PYTHON)" -m compileall -q src scripts tests services/transcription/src services/transcription/tests

compose-check:
	./scripts/compose-check.sh

publication-check:
	"$(PYTHON)" scripts/publication-check.py

publication-candidate-check:
	"$(PYTHON)" scripts/check-publication-candidate.py --expected-head "$$(git rev-parse HEAD)" --publication-ref "$$(git symbolic-ref HEAD)"

secret-check:
	@set -eu; \
	test "$$("$(GITLEAKS)" version)" = '8.30.1'; \
	report="$$(mktemp)"; trap 'rm -f "$$report"' EXIT; \
	if ! "$(GITLEAKS)" git --redact --no-banner --log-opts='--all' > "$$report" 2>&1; then \
		echo 'Secret scan blocked. Maintainer must inspect privately; no matched values or artifacts uploaded.' >&2; exit 1; \
	fi

test-postgres:
	@test -n "$$CASE_REVIEW_POSTGRES_TEST_DSN" || { echo 'Set CASE_REVIEW_POSTGRES_TEST_DSN to a synthetic PostgreSQL 17 + pgvector database.' >&2; exit 1; }
	@set -eu; report="$$(mktemp)"; trap 'rm -f "$$report"' EXIT; \
	"$(PYTHON)" -m pytest -q tests/test_review_bench_v2_postgres.py \
		--deselect tests/test_review_bench_v2_postgres.py::test_live_learned_dense_only_paraphrase_and_unsupported_abstention \
		--junitxml="$$report"; \
	"$(PYTHON)" scripts/check-postgres-test-report.py "$$report"

deployment-check: compose-check
	"$(PYTHON)" -c 'import json, pathlib, subprocess, tempfile; \
		temporary = tempfile.TemporaryDirectory(); root = pathlib.Path(temporary.name) / "uncreated node"; \
		result = subprocess.run(["./install", "preflight", "--root", str(root), "--models", "none", "--json"], capture_output=True, text=True, timeout=90); \
		payload = json.loads(result.stdout); \
		assert payload["schema_version"] == 1; assert payload["checks"]; \
		blocked = any(row["blocking"] and row["state"] != "pass" for row in payload["checks"]); \
		assert payload["ready"] is (not blocked); assert result.returncode == (1 if blocked else 0); \
		assert all(row["remedy"] for row in payload["checks"] if row["blocking"] and row["state"] != "pass"); \
		assert not root.exists(); temporary.cleanup()'

# Full PR gates: no browser journeys; missing PostgreSQL/scanner prerequisites fail.
# Publication checks cover both the working tree and the complete committed candidate.
# Ignore inherited test selections. PostgreSQL runs separately with its checked report,
# as in CI, rather than accidentally enabling database tests in every xdist worker.
check-fast: export PYTEST_ADDOPTS :=
check-fast: compile deployment-check publication-check publication-candidate-check secret-check test-fast test-transcription test-postgres

check: compile compose-check publication-check test test-transcription
