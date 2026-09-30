# Sourced by the spike's scripts (ADR-0008 amendment, 2026-09-30): the region,
# the name of everything they create, the tag that marks it, and the helpers
# they share. Every resource name is fixed here, so a teardown deletes only what
# a deploy created.
set -euo pipefail

export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""

# Fails unless each tool is on PATH. Every script needs aws, jq and python3;
# deploy.sh and invoke.sh ask for the rest they use.
require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" > /dev/null || { echo "$0 needs $tool on PATH" >&2; exit 1; }
  done
}
require_tools aws jq python3

NAME=aqa-spike-hosted-chromium
TAG_KEY=aqa-spike
TAG_VALUE=hosted-chromium
IAM_PATH=/aqa-spike/
LOG_PREFIX=/aqa-spike/hosted-chromium
# Every candidate gets 2 GB: the Fargate task and the MicroVM's baseline get
# 1 vCPU with it, and a Lambda function about 1.16, since its CPU follows its
# memory (1,769 MB is one vCPU).
MEMORY_MB=2048
FARGATE_CPU=1024
BUDGET_USD=10
MICROVM_BASE_IMAGE=arn:aws:lambda:$AWS_REGION:aws:microvm-image:al2023-1
MICROVM_CONNECTOR=arn:aws:lambda:$AWS_REGION:aws:network-connector:aws-network-connector
# The longest a MicroVM may live, running or suspended: an invoke that fails
# before it terminates its MicroVM leaves one that ends by itself.
MICROVM_MAX_SECONDS=900

spike_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d)
# A script that must undo more on the way out, as invoke.sh does, redefines it.
cleanup() {
  rm -rf "$work"
}
trap cleanup EXIT

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
