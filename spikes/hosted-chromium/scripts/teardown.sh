#!/usr/bin/env bash
# Deletes everything budget.sh, deploy.sh and invoke.sh create (ADR-0008
# amendment, 2026-09-30), then fails unless each of them is gone. It finds what
# to delete by listing the fixed names in common.sh, so it touches only the
# spike's own resources, and it works after a partial deploy too. The budget
# alarm goes last.
#
# AWS may create the ECS service-linked role (AWSServiceRoleForECS) with the
# first cluster. It costs nothing, serves any later use of ECS, and stays.
#
# Usage: spikes/hosted-chromium/scripts/teardown.sh
source "$(dirname "$0")/common.sh"

account=$(account_id)
bucket=$(bucket_name)

# Each prints the spike's resources of one kind that exist now.
microvms() {
  aws lambda-microvms list-microvms --output text --query \
    "items[?state!='TERMINATED' && contains(imageArn, ':microvm-image:$NAME')].microvmId"
}
microvm_images() {
  aws lambda-microvms list-microvm-images --output text \
    --query "items[?name=='$NAME' && state!='DELETED'].name"
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
  aws ecs list-task-definitions --family-prefix "$NAME" --status "$1" \
    --query taskDefinitionArns --output text
}
security_groups() {
  aws ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" \
    --query 'SecurityGroups[].GroupId' --output text
}
repositories() {
  aws ecr describe-repositories --output text \
    --query "repositories[?starts_with(repositoryName, '$NAME-')].repositoryName"
}
buckets() {
  aws s3api list-buckets --query "Buckets[?Name=='$bucket'].Name" --output text
}
log_groups() {
  aws logs describe-log-groups --log-group-name-prefix "$LOG_PREFIX/" \
    --query 'logGroups[].logGroupName' --output text
}
roles() {
  aws iam list-roles --path-prefix "$IAM_PATH" --output text \
    --query "Roles[?starts_with(RoleName, '$NAME-')].RoleName"
}
budgets() {
  aws budgets describe-budgets --account-id "$account" --output text \
    --query "Budgets[?BudgetName=='$NAME'].BudgetName"
}

for microvm in $(microvms); do
  aws lambda-microvms terminate-microvm --microvm-identifier "$microvm" > /dev/null
done
for image in $(microvm_images); do
  aws lambda-microvms delete-microvm-image --image-identifier "$image" > /dev/null
done
for function in $(functions); do
  aws lambda delete-function --function-name "$function"
done
for cluster in $(clusters); do
  read -ra tasks <<< "$(aws ecs list-tasks --cluster "$cluster" --query taskArns --output text)"
  if [ "${#tasks[@]}" -gt 0 ]; then
    for task in "${tasks[@]}"; do
      aws ecs stop-task --cluster "$cluster" --task "$task" > /dev/null
    done
    aws ecs wait tasks-stopped --cluster "$cluster" --tasks "${tasks[@]}"
  fi
  aws ecs delete-cluster --cluster "$cluster" > /dev/null
done
for definition in $(task_definitions ACTIVE); do
  aws ecs deregister-task-definition --task-definition "$definition" > /dev/null
done
read -ra inactive <<< "$(task_definitions INACTIVE)"
if [ "${#inactive[@]}" -gt 0 ]; then
  aws ecs delete-task-definitions --task-definitions "${inactive[@]}" > /dev/null
fi
for group in $(security_groups); do
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
for repository in $(repositories); do
  aws ecr delete-repository --repository-name "$repository" --force > /dev/null
done
for name in $(buckets); do
  aws s3 rb "s3://$name" --force > /dev/null
done
for group in $(log_groups); do
  aws logs delete-log-group --log-group-name "$group"
done
for role in $(roles); do
  for policy in $(aws iam list-role-policies --role-name "$role" --query PolicyNames --output text); do
    aws iam delete-role-policy --role-name "$role" --policy-name "$policy"
  done
  aws iam delete-role --role-name "$role"
done
for budget in $(budgets); do
  aws budgets delete-budget --account-id "$account" --budget-name "$budget"
done

remaining=""
for kind in microvms microvm_images functions clusters "task_definitions ACTIVE" \
  security_groups repositories buckets log_groups roles budgets; do
  found=$($kind)
  if [ -n "$found" ]; then
    remaining+="  ${kind%% *}: $found"$'\n'
  fi
done
if [ -n "$remaining" ]; then
  printf 'still there after the teardown:\n%s' "$remaining" >&2
  exit 1
fi
echo "teardown complete: nothing of $NAME remains in $AWS_REGION"
