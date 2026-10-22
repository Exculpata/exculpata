#!/usr/bin/env bash
# Bound CI package acquisition, then install downloaded archives offline.
set -euo pipefail

dir="${APT_ARCHIVES:-$RUNNER_TEMP/apt-archives}"
mkdir -p "$dir/partial"
options=(
  -o Acquire::Retries=3
  -o Acquire::http::Timeout=20
  -o Acquire::https::Timeout=20
  -o Dir::Cache::archives="$dir"
  -o APT::Keep-Downloaded-Packages=true
)

acquire() {
  sudo timeout --signal=TERM --kill-after=10s 60s apt-get "${options[@]}" update --error-on=any &&
    sudo timeout --signal=TERM --kill-after=10s 180s apt-get "${options[@]}" \
      install --yes --no-install-recommends --download-only "$@"
}

if ! acquire "$@"; then
  echo 'APT acquisition failed; retrying once.'
  acquire "$@"
fi

# Apt verifies cached .deb files against freshly downloaded signed indexes.
sudo apt-get "${options[@]}" install --yes --no-install-recommends --no-download "$@"
sudo chown -R "$(id -u):$(id -g)" "$dir"
