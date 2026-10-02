#!/usr/bin/env bash
# The dependency audit (ADR-0029): runs osv-scanner on each lockfile it is given
# and applies CONSTRAINTS.md's "Dependency audit" row. An advisory group scoring
# CVSS 7.0 or more, or with no score, fails; every finding is listed with its
# lockfile and score. Waivers are the root osv-scanner.toml's entries, under that
# row's rule: --config makes it the only waiver file osv-scanner reads, where it
# would otherwise read one next to each lockfile. tests/test_constraints.py runs
# this script against a fake scanner.
#
# Usage, from the repository root: audit-lockfile.sh <osv-scanner> <lockfile>...
#
# Each lockfile gets a scan of its own: given two at once, osv-scanner passes
# over a file that parses to nothing, and fails on it alone (exit 128).
# osv-scanner exits 1 on an advisory of any severity, so the script judges its
# JSON, and fails closed: any other nonzero exit is passed through, JSON
# without the expected keys is a jq error, an exit 1 whose JSON gives no rows
# fails, and so does a call with no lockfile. A failing advisory doesn't stop
# the next lockfile from being judged, so every finding is listed; a scanner
# error or an unreadable report stops the script at once.
set -euo pipefail

scanner=$1
shift
if [ "$#" -eq 0 ]; then
  echo "::error::no lockfile to audit"
  exit 2
fi
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

failed=0
for lockfile in "$@"; do
  # A scan that writes no report must not be judged by the one before's.
  rm -f "$work/osv.json"
  status=0
  "$scanner" scan source --config osv-scanner.toml --lockfile "$lockfile" \
    --format json --output-file "$work/osv.json" || status=$?
  if [ "$status" -gt 1 ]; then exit "$status"; fi

  # One row per advisory group: the package, the group's IDs and its highest
  # CVSS score, or null when the advisory has none.
  jq '[.results[].packages[] | .package as $p | .groups[]
      | {package: "\($p.name) \($p.version)", ids: (.ids | join(", ")),
         score: (.max_severity | tonumber? // null)}]' \
    "$work/osv.json" > "$work/findings.json"
  jq -r --arg lockfile "$lockfile" \
    '.[] | "\($lockfile): \(.package): \(.ids) (CVSS \(.score // "none"))"' \
    "$work/findings.json"

  if [ "$status" -eq 1 ] && jq -e 'length == 0' "$work/findings.json" > /dev/null; then
    echo "::error::osv-scanner reported findings in $lockfile that the filter read no rows from"
    failed=1
  elif ! jq -e 'all(.score != null and .score < 7)' "$work/findings.json" > /dev/null; then
    echo "::error::$lockfile has a high or critical advisory, or one without a score (listed above)"
    failed=1
  fi
done
exit "$failed"
