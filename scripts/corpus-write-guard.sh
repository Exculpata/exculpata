#!/usr/bin/env bash
set -eu

repository=$(git rev-parse --show-toplevel)
cd "$repository"
case "${1:-}" in
  prepare)
    chmod -R a-w -- benchmarks/corpus
    ;;
  check)
    git diff --exit-code -- benchmarks/corpus
    status=$(git status --porcelain --untracked-files=all -- benchmarks/corpus)
    if [ -n "$status" ]; then
      printf 'Corpus changed during testing:\n%s\n' "$status" >&2
      exit 1
    fi
    ;;
  *)
    echo 'Usage: bash scripts/corpus-write-guard.sh prepare|check' >&2
    exit 2
    ;;
esac
