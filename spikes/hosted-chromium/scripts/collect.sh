#!/usr/bin/env bash
# The account's billed usage in us-east-1, by usage type and day, from Cost
# Explorer (ADR-0008 amendment, 2026-09-30), printed as JSON. It is the only
# source for a MicroVM's snapshot storage (GB-hours) and snapshot reads (GB):
# no MicroVM API reports a snapshot's size. Cost Explorer lags by about a day,
# and each request costs $0.01.
# https://docs.aws.amazon.com/cli/latest/reference/ce/get-cost-and-usage.html
#
# Usage: spikes/hosted-chromium/scripts/collect.sh <first day> <day after the last>
#        with days as YYYY-MM-DD
source "$(dirname "$0")/common.sh"

first=${1:-}
after=${2:-}
day='^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
# Each request costs money, so a malformed one is refused before it's made.
if [ "$#" -ne 2 ] || [[ ! $first =~ $day || ! $after =~ $day || ! $first < $after ]]; then
  echo "usage: $0 <first day> <day after the last>, as YYYY-MM-DD, in that order" >&2
  exit 2
fi

aws ce get-cost-and-usage --time-period "Start=$first,End=$after" \
  --granularity DAILY --metrics UsageQuantity UnblendedCost \
  --group-by Type=DIMENSION,Key=USAGE_TYPE \
  --filter "{\"Dimensions\": {\"Key\": \"REGION\", \"Values\": [\"$AWS_REGION\"]}}" \
  --output json
