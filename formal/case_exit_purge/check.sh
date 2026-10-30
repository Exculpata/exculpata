#!/bin/sh
set -eu
command -v java >/dev/null 2>&1 || { echo 'Java is missing: put Java 21 on PATH.' >&2; exit 1; }
[ -n "${TLA2TOOLS_JAR:-}" ] && [ -r "$TLA2TOOLS_JAR" ] || {
    echo 'Set TLA2TOOLS_JAR to the readable TLA+ tools v1.8.0 jar.' >&2; exit 1;
}
jar=$(realpath "$TLA2TOOLS_JAR")
echo 'fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e  '"$jar" | sha256sum -c -
model=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cp "$model/CaseExitPurge.tla" "$work/"
cd "$work"
if [ "${1:-}" = mutants ]; then
    for invariant in S1 S1b S1c S2 S3 S4 S5 S6; do
        printf 'SPECIFICATION Spec\nCONSTANTS MaxExits = 2\nMutant = "%s"\nINVARIANT %s\n' "$invariant" "$invariant" > mutant.cfg
        status=0
        java -XX:+UseParallelGC -cp "$jar" tlc2.TLC -workers 1 -config mutant.cfg CaseExitPurge.tla > result.log 2>&1 || status=$?
        cat result.log
        [ "$status" -ne 0 ] && grep -F "Error: Invariant $invariant is violated." result.log >/dev/null || {
            echo "Mutant $invariant did not produce its expected violation." >&2; exit 1;
        }
        echo "MUTANT $invariant: expected violation detected"
    done
else
    java -XX:+UseParallelGC -cp "$jar" tlc2.TLC -workers 1 -config "$model/CaseExitPurge.cfg" CaseExitPurge.tla
fi
