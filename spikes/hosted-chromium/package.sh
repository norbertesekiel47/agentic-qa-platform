#!/usr/bin/env bash
# Builds one candidate's package of the spike's trial locally (ADR-0008
# amendment, 2026-09-30), from HEAD, so every measurement traces to a commit:
#   - the image aqa-spike-trial:<candidate>, for fargate, lambda or microvm;
#   - for microvm, also the zip Lambda builds its MicroVM image from,
#     build/microvm-<commit>.zip next to this script.
# The build context holds only what the trial installs: the workspace's
# pyproject files, its lock, and the sources of the packages the trial needs.
#
# Usage: spikes/hosted-chromium/package.sh fargate|lambda|microvm
set -euo pipefail

candidate=${1:-}
case "$candidate" in
  fargate | lambda | microvm) ;;
  *)
    echo "usage: $0 fargate|lambda|microvm" >&2
    exit 2
    ;;
esac

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$repo"
paths=(
  pyproject.toml uv.lock .python-version
  packages/core packages/runner/pyproject.toml packages/runner/src
  packages/cli/pyproject.toml bench/harness/pyproject.toml
  spikes/hosted-chromium/pyproject.toml spikes/hosted-chromium/src
  spikes/hosted-chromium/Dockerfile
)
if [ -n "$(git status --porcelain -- "${paths[@]}")" ]; then
  echo "commit these first: the package is built from HEAD" >&2
  git status --short -- "${paths[@]}" >&2
  exit 1
fi

commit=$(git rev-parse --short=12 HEAD)
build=spikes/hosted-chromium/build
context=$build/context
rm -rf "$context"
mkdir -p "$context"
git archive HEAD -- "${paths[@]}" | tar -x -C "$context"
mv "$context/spikes/hosted-chromium/Dockerfile" "$context/Dockerfile"

docker build --platform linux/arm64 --target "$candidate" \
  --tag "aqa-spike-trial:$candidate" \
  --label "org.opencontainers.image.revision=$commit" "$context"

if [ "$candidate" = microvm ]; then
  zip="$repo/$build/microvm-$commit.zip"
  rm -f "$zip"
  (cd "$context" && python3 -m zipfile -c "$zip" Dockerfile .python-version \
    pyproject.toml uv.lock packages bench spikes)
  echo "$zip"
fi
