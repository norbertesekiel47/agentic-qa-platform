#!/bin/sh
# Benchmark feature flags (ADR-0022). Ours, not upstream; used by both images.
# Validates BENCH_FLAGS (comma-separated flag ids, each 4 lowercase letters or
# digits) and prints the ids as a JSON array. Any other value exits 1, so a
# container never starts with a flag set it can't honour.
set -eu
set -f # no globbing while the list is split
json=""
IFS=,
for id in ${BENCH_FLAGS:-}; do
  case "$id" in
    [a-z0-9][a-z0-9][a-z0-9][a-z0-9]) json="$json${json:+,}\"$id\"" ;;
    *)
      echo "BENCH_FLAGS: invalid flag id '$id' (want 4 lowercase letters or digits)" >&2
      exit 1
      ;;
  esac
done
printf '[%s]\n' "$json"
