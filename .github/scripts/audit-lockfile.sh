#!/usr/bin/env bash
# The dependency audit (ADR-0029): runs osv-scanner on a lockfile and applies
# CONSTRAINTS.md's "Dependency audit" row. An advisory group scoring CVSS 7.0
# or more, or with no score, fails; every finding is listed with its score.
# Waivers are osv-scanner.toml entries next to the lockfile, under that row's
# rule. tests/test_constraints.py runs this script against a fake scanner.
#
# Usage: audit-lockfile.sh <osv-scanner> <lockfile>
#
# osv-scanner exits 1 on an advisory of any severity, so the script judges its
# JSON, and fails closed: any other nonzero exit is passed through, JSON
# without the expected keys is a jq error, and an exit 1 whose JSON gives no
# rows fails.
set -euo pipefail

scanner=$1
lockfile=$2
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

status=0
"$scanner" scan source --lockfile "$lockfile" --format json \
  --output-file "$work/osv.json" || status=$?
if [ "$status" -gt 1 ]; then exit "$status"; fi

# One row per advisory group: the package, the group's IDs and its highest
# CVSS score, or null when the advisory has none.
jq '[.results[].packages[] | .package as $p | .groups[]
    | {package: "\($p.name) \($p.version)", ids: (.ids | join(", ")),
       score: (.max_severity | tonumber? // null)}]' \
  "$work/osv.json" > "$work/findings.json"
jq -r '.[] | "\(.package): \(.ids) (CVSS \(.score // "none"))"' "$work/findings.json"

if [ "$status" -eq 1 ] && jq -e 'length == 0' "$work/findings.json" > /dev/null; then
  echo "::error::osv-scanner reported findings that the filter read no rows from"
  exit 1
fi
if ! jq -e 'all(.score != null and .score < 7)' "$work/findings.json" > /dev/null; then
  echo "::error::$lockfile has a high or critical advisory, or one without a score (listed above)"
  exit 1
fi
