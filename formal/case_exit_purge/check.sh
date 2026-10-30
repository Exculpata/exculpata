#!/bin/sh
set -eu
command -v java >/dev/null 2>&1 || { echo 'Java 21 or newer is required: java is missing from PATH.' >&2; exit 1; }
java_version=$(java -version 2>&1) || { echo 'Java 21 or newer is required: java -version failed.' >&2; exit 1; }
java_major=$(printf '%s\n' "$java_version" | sed -n 's/.*version "\([0-9][0-9]*\)[.\"-].*/\1/p' | head -n 1)
if [ -z "$java_major" ] || [ "$java_major" -lt 21 ]; then
    echo 'Java 21 or newer is required: put a supported JDK on PATH (version unrecognized or too old).' >&2
    exit 1
fi
# v1.8.0 is a rolling pre-release; the release tag is rebuilt over time.
release='TLA+ tools v1.8.0 rolling pre-release'
download='https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar'
[ -n "${TLA2TOOLS_JAR:-}" ] && [ -r "$TLA2TOOLS_JAR" ] || {
    echo "Set TLA2TOOLS_JAR to a readable $release jar. Download: $download" >&2; exit 1;
}
jar=$(realpath "$TLA2TOOLS_JAR")
echo "Default pin: $release, TLC2 Version 2026.10.28.014557 (rev: 56d37a9)."
sha256=${TLA2TOOLS_SHA256-fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e}
if [ "${TLA2TOOLS_SHA256+x}" = x ]; then
    echo "Explicit TLA2TOOLS_SHA256 override: $sha256 (default build pin overridden)." >&2
fi
if [ "$sha256" = skip ]; then
    echo 'WARNING: jar SHA-256 verification skipped by explicit request.' >&2
else
    printf '%s  %s\n' "$sha256" "$jar" | sha256sum -c - || {
        echo "Jar does not match the selected SHA-256. Download: $download" >&2
        echo 'For another reviewed build, explicitly set TLA2TOOLS_SHA256 to its hash or skip.' >&2
        exit 1
    }
fi
model=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cp "$model/CaseExitPurge.tla" "$work/"
cd "$work"
if [ "${1:-}" = mutants ]; then
    for mutant in S1 S1b S1b_name S1c S1c_mode_flip S2 S3 S4 S5 S6; do
        invariant=${mutant%%_*}
        printf 'SPECIFICATION Spec\nCONSTANTS MaxExits = 2\nMutant = "%s"\nINVARIANT %s\n' "$mutant" "$invariant" > mutant.cfg
        status=0
        java -XX:+UseParallelGC -cp "$jar" tlc2.TLC -workers 1 -config mutant.cfg CaseExitPurge.tla > result.log 2>&1 || status=$?
        cat result.log
        [ "$status" -ne 0 ] && grep -F "Error: Invariant $invariant is violated." result.log >/dev/null || {
            echo "Mutant $mutant did not produce its expected $invariant violation." >&2; exit 1;
        }
        echo "MUTANT $mutant: expected $invariant violation detected"
    done
else
    java -XX:+UseParallelGC -cp "$jar" tlc2.TLC -workers 1 -config "$model/CaseExitPurge.cfg" CaseExitPurge.tla
fi
