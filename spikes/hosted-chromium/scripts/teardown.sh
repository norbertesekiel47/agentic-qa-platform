#!/usr/bin/env bash
# Deletes everything budget.sh, deploy.sh and invoke.sh create (ADR-0008
# amendment, 2026-09-30), then fails unless each of them is gone. It finds what
# to delete by listing the fixed names in common.sh, so it touches only the
# spike's own resources, and it works after a partial deploy too. The budget
# alarm goes last, once everything else is gone. A failed listing or delete
# stops it where it fails.
#
# AWS may create the ECS service-linked role (AWSServiceRoleForECS) with the
# first cluster. It costs nothing, serves any later use of ECS, and stays.
#
# Usage: spikes/hosted-chromium/scripts/teardown.sh
source "$(dirname "$0")/common.sh"

load_account
repositories_list=$(jmespath_list "${REPOSITORIES[@]}")
roles_list=$(jmespath_list "${ROLES[@]}")
log_groups_list=$(jmespath_list "${LOG_GROUPS[@]}")

# Each prints the spike's resources of one kind that exist now, matching the
# exact names in common.sh. A caller assigns the output to a variable first,
# so a failed listing stops the script instead of reading as "none".
microvms() {
  aws lambda-microvms list-microvms --output text --query \
    "items[?state!='TERMINATED' && imageArn=='arn:aws:lambda:$AWS_REGION:$account:microvm-image:$NAME'].microvmId"
}
microvm_images() {
  aws lambda-microvms list-microvm-images --output text \
    --query "items[?name=='$NAME' && state!='DELETED'].imageArn"
}
functions() {
  aws lambda list-functions --output text \
    --query "Functions[?FunctionName=='$NAME'].FunctionName"
}
clusters() {
  aws ecs describe-clusters --clusters "$NAME" --output text \
    --query "clusters[?status=='ACTIVE'].clusterName"
}
task_definitions() {
  aws ecs list-task-definitions --family-prefix "$NAME" --status "$1" --output text \
    --query "taskDefinitionArns[?contains(@, ':task-definition/$NAME:')]"
}
security_groups() {
  aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$NAME" "Name=tag:$TAG_KEY,Values=$TAG_VALUE" \
    --query 'SecurityGroups[].GroupId' --output text
}
repositories() {
  aws ecr describe-repositories --output text \
    --query "repositories[?contains($repositories_list, repositoryName)].repositoryName"
}
buckets() {
  aws s3api list-buckets --query "Buckets[?Name=='$bucket'].Name" --output text
}
log_groups() {
  aws logs describe-log-groups --log-group-name-prefix "$LOG_PREFIX/" --output text \
    --query "logGroups[?contains($log_groups_list, logGroupName)].logGroupName"
}
roles() {
  aws iam list-roles --path-prefix "$IAM_PATH" --output text \
    --query "Roles[?contains($roles_list, RoleName)].RoleName"
}
# DescribeBudgets answers NotFoundException for an account without budgets,
# so this asks for the one budget by name, and only that answer means gone.
budgets() {
  if aws budgets describe-budget --account-id "$account" --budget-name "$NAME" \
    --query Budget.BudgetName --output text 2>"$work/budget.err"; then
    return 0
  fi
  if ! grep -q NotFoundException "$work/budget.err"; then
    cat "$work/budget.err" >&2
    return 1
  fi
}

found=$(microvms)
for microvm in $found; do
  aws lambda-microvms terminate-microvm --microvm-identifier "$microvm" > /dev/null
done
found=$(microvm_images)
for image in $found; do
  aws lambda-microvms delete-microvm-image --image-identifier "$image" > /dev/null
done
found=$(functions)
for function in $found; do
  aws lambda delete-function --function-name "$function"
done
found=$(clusters)
for cluster in $found; do
  tasks=$(aws ecs list-tasks --cluster "$cluster" --query taskArns --output text)
  read -ra running <<< "$tasks"
  if [ "${#running[@]}" -gt 0 ]; then
    for task in "${running[@]}"; do
      aws ecs stop-task --cluster "$cluster" --task "$task" > /dev/null
    done
    aws ecs wait tasks-stopped --cluster "$cluster" --tasks "${running[@]}"
  fi
  aws ecs delete-cluster --cluster "$cluster" > /dev/null
done
found=$(task_definitions ACTIVE)
for definition in $found; do
  aws ecs deregister-task-definition --task-definition "$definition" > /dev/null
done
found=$(task_definitions INACTIVE)
read -ra inactive <<< "$found"
if [ "${#inactive[@]}" -gt 0 ]; then
  # A revision it can't delete comes back in `failures`, with exit code 0.
  failed=$(aws ecs delete-task-definitions --task-definitions "${inactive[@]}" \
    --query 'failures[].arn' --output text)
  if [ -n "$failed" ]; then
    echo "ECS didn't delete these task definitions: $failed" >&2
    exit 1
  fi
fi
found=$(security_groups)
for group in $found; do
  # A stopped task's network interface can hold the group for a few minutes.
  for attempt in $(seq 36); do
    if aws ec2 delete-security-group --group-id "$group" 2>"$work/group.err"; then
      break
    fi
    if ! grep -q DependencyViolation "$work/group.err" || [ "$attempt" = 36 ]; then
      cat "$work/group.err" >&2
      exit 1
    fi
    sleep 5
  done
done
found=$(repositories)
for repository in $found; do
  aws ecr delete-repository --repository-name "$repository" --force > /dev/null
done
found=$(buckets)
for name in $found; do
  aws s3 rb "s3://$name" --force > /dev/null
done
found=$(log_groups)
for group in $found; do
  aws logs delete-log-group --log-group-name "$group"
done
found=$(roles)
for role in $found; do
  policies=$(aws iam list-role-policies --role-name "$role" --query PolicyNames --output text)
  for policy in $policies; do
    aws iam delete-role-policy --role-name "$role" --policy-name "$policy"
  done
  aws iam delete-role --role-name "$role"
done
# Adds to `left` what a listing still shows.
still() {
  local listed
  listed=$("$@")
  if [ -n "$listed" ]; then
    left+="  $*: $listed"$'\n'
  fi
}

# Some deletes finish later (a MicroVM image is DELETING, a MicroVM
# TERMINATING), and IAM's listings lag, so the check gives them five minutes.
# The budget alarm stays until nothing else is left.
for attempt in $(seq 30); do
  left=""
  still microvms
  still microvm_images
  still functions
  still clusters
  still task_definitions ACTIVE
  still task_definitions INACTIVE
  still security_groups
  still repositories
  still buckets
  still log_groups
  still roles
  if [ -z "$left" ]; then
    break
  fi
  if [ "$attempt" = 30 ]; then
    printf 'still there after the teardown, so the budget alarm stays:\n%s' "$left" >&2
    exit 1
  fi
  sleep 10
done

found=$(budgets)
for budget in $found; do
  aws budgets delete-budget --account-id "$account" --budget-name "$budget"
done
left=""
still budgets
if [ -n "$left" ]; then
  printf 'still there after the teardown:\n%s' "$left" >&2
  exit 1
fi
echo "teardown complete: nothing of $NAME remains in $AWS_REGION"
