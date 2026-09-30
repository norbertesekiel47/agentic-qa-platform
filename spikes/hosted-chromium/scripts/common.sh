# Sourced by the spike's scripts (ADR-0008 amendment, 2026-09-30): the region,
# the name of everything they create, the tag that marks it, and the helpers
# they share. Every resource name is fixed here, so a teardown deletes only what
# a deploy created.
set -euo pipefail

export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
for tool in aws docker jq python3 curl; do
  command -v "$tool" > /dev/null || { echo "$0 needs $tool on PATH" >&2; exit 1; }
done

NAME=aqa-spike-hosted-chromium
TAG_KEY=aqa-spike
TAG_VALUE=hosted-chromium
IAM_PATH=/aqa-spike/
LOG_PREFIX=/aqa-spike/hosted-chromium
CANDIDATES=(lambda fargate microvm)
# Every candidate gets 2 GB and 1 vCPU (a Lambda function's CPU follows its
# memory: 1,769 MB is one vCPU), so they compare at one size.
MEMORY_MB=2048
FARGATE_CPU=1024
BUDGET_USD=10
MICROVM_BASE_IMAGE=arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1
MICROVM_CONNECTOR=arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector
# The longest a MicroVM may live, running or suspended: an invoke that fails
# before it terminates its MicroVM leaves one that ends by itself.
MICROVM_MAX_SECONDS=900

spike_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

usage_candidate() {
  echo "usage: $0 lambda|fargate|microvm${1:+ $1}" >&2
  exit 2
}

is_candidate() {
  case "${1:-}" in
    lambda | fargate | microvm) return 0 ;;
    *) return 1 ;;
  esac
}

account_id() {
  aws sts get-caller-identity --query Account --output text
}

bucket_name() {
  echo "$NAME-$(account_id)"
}

log_group() {
  echo "$LOG_PREFIX/$1"
}

role_name() {
  echo "$NAME-$1"
}

now() {
  python3 -c 'import time; print(f"{time.time():.3f}")'
}

# Fails unless the budget alarm exists, so no other resource is ever created
# before it.
require_budget() {
  if ! aws budgets describe-budget --account-id "$(account_id)" \
    --budget-name "$NAME" > /dev/null 2>"$work/budget.err"; then
    cat "$work/budget.err" >&2
    echo "no budget alarm $NAME: run budget.sh first" >&2
    exit 1
  fi
}
