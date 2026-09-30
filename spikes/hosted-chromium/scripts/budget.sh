#!/usr/bin/env bash
# Creates the spike's budget alarm, the first resource the spike creates
# (ADR-0008 amendment, 2026-09-29): a monthly cost budget for the whole account
# that emails at 50% and 100% of actual spend. deploy.sh refuses to run
# without it. Budget alarms without actions cost nothing, but they lag: AWS
# updates spend up to three times a day, so the scripts also cap how long a
# candidate can run.
# https://docs.aws.amazon.com/cli/latest/reference/budgets/create-budget.html
#
# Usage: spikes/hosted-chromium/scripts/budget.sh <email>
source "$(dirname "$0")/common.sh"

email=${1:-}
if [[ ! $email =~ ^[^@[:space:]]+@[^@[:space:]]+$ ]]; then
  echo "usage: $0 <email for the alarm>" >&2
  exit 2
fi

load_account
jq -n --arg name "$NAME" --arg amount "$BUDGET_USD" \
  '{BudgetName: $name, BudgetType: "COST", TimeUnit: "MONTHLY",
    BudgetLimit: {Amount: $amount, Unit: "USD"}}' > "$work/budget.json"
jq -n --arg email "$email" \
  '[50, 100] | map({
     Notification: {NotificationType: "ACTUAL", ComparisonOperator: "GREATER_THAN",
                    Threshold: ., ThresholdType: "PERCENTAGE"},
     Subscribers: [{SubscriptionType: "EMAIL", Address: $email}]})' \
  > "$work/notifications.json"

aws budgets create-budget --account-id "$account" \
  --budget "file://$work/budget.json" \
  --notifications-with-subscribers "file://$work/notifications.json" \
  --resource-tags "Key=$TAG_KEY,Value=$TAG_VALUE"
echo "budget alarm $NAME: \$$BUDGET_USD a month, emails at 50% and 100%"
