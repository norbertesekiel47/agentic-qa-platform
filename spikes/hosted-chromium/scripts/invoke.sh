#!/usr/bin/env bash
# One trial on a deployed candidate (ADR-0008 amendment, 2026-09-30), printed
# as one line of JSON: the trial's report, when the request went out and when
# the answer came back (this machine's clock), and the platform's own record of
# the run. The README's measurement plan says what each field measures.
#
#   cold  lambda:  after a configuration change, which Lambda answers from a
#                  new execution environment;
#         fargate: every task;
#         microvm: the first MicroVM after deploy.sh built the image.
#   warm  lambda:  the next invocation, which may reuse the last environment;
#         microvm: any later MicroVM: still a new VM, from the same snapshot.
#         A Fargate task has no warm start.
#
# Usage: spikes/hosted-chromium/scripts/invoke.sh lambda|fargate|microvm cold|warm
source "$(dirname "$0")/common.sh"

candidate=${1:-}
start=${2:-}
is_candidate "$candidate" || usage_candidate "cold|warm"
case $start in cold | warm) ;; *) usage_candidate "cold|warm" ;; esac
require_budget

# Prints the measurement, with the trial's report from $work/trial.json and the
# platform's record given as JSON.
emit() {
  jq -nc --arg candidate "$candidate" --arg start "$start" \
    --argjson requested "$requested" --argjson answered "$answered" \
    --slurpfile trial "$work/trial.json" --argjson platform "$1" \
    '{candidate: $candidate, start: $start, requested_at: $requested,
      answered_at: $answered, trial: $trial[0], platform: $platform}'
}

invoke_lambda() {
  if [ "$start" = cold ]; then
    aws lambda update-function-configuration --function-name "$NAME" \
      --environment "Variables={AQA_SPIKE_START=$(now)}" > /dev/null
    aws lambda wait function-updated-v2 --function-name "$NAME"
  fi
  requested=$(now)
  aws lambda invoke --function-name "$NAME" --payload '{}' \
    --cli-binary-format raw-in-base64-out --log-type Tail --cli-read-timeout 150 \
    --output json "$work/trial.json" > "$work/invoke.json"
  answered=$(now)
  if jq -e 'has("FunctionError")' "$work/invoke.json" > /dev/null; then
    echo "the trial failed on Lambda:" >&2
    cat "$work/trial.json" >&2
    exit 1
  fi
  # The log's tail ends with Lambda's REPORT line: durations and memory used.
  jq -r .LogResult "$work/invoke.json" | python3 -m base64 -d > "$work/log.txt"
  emit "$(jq -n --arg report "$(grep '^REPORT' "$work/log.txt")" '{report: $report}')"
}

invoke_fargate() {
  if [ "$start" = warm ]; then
    echo "every Fargate task starts cold: there is no warm start to measure" >&2
    exit 2
  fi
  local subnets group task
  subnets=$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true \
    --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
  group=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" \
    --query 'SecurityGroups[].GroupId' --output text)
  requested=$(now)
  task=$(aws ecs run-task --cluster "$NAME" --launch-type FARGATE --task-definition "$NAME" \
    --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$group],assignPublicIp=ENABLED}" \
    --query 'tasks[0].taskArn' --output text)
  aws ecs wait tasks-stopped --cluster "$NAME" --tasks "$task"
  answered=$(now)
  aws ecs describe-tasks --cluster "$NAME" --tasks "$task" --query 'tasks[0]' \
    --output json > "$work/task.json"
  if ! jq -e '.containers[0].exitCode == 0' "$work/task.json" > /dev/null; then
    echo "the trial's task failed:" >&2
    jq . "$work/task.json" >&2
    exit 1
  fi
  # The trial prints its report as one line of JSON, which can reach the log a
  # few seconds after the task stops; Chromium may log other lines around it.
  for _ in $(seq 30); do
    aws logs get-log-events --log-group-name "$(log_group fargate)" \
      --log-stream-name "trial/trial/${task##*/}" --start-from-head \
      --query 'events[]' --output json > "$work/events.json"
    jq -c '[.[] | select(.message | startswith("{"))] | last' "$work/events.json" \
      > "$work/event.json"
    [ "$(cat "$work/event.json")" = null ] || break
    sleep 1
  done
  jq '.message | fromjson' "$work/event.json" > "$work/trial.json"
  emit "$(jq -n --slurpfile task "$work/task.json" --slurpfile event "$work/event.json" \
    '{task: $task[0], reported_at: ($event[0].timestamp / 1000)}')"
}

invoke_microvm() {
  local image role run id endpoint
  image=$(aws lambda-microvms get-microvm-image --image-identifier "$NAME" \
    --query imageArn --output text)
  role=$(aws iam get-role --role-name "$(role_name microvm)" --query Role.Arn --output text)
  requested=$(now)
  # With no traffic for a minute the MicroVM suspends, and a suspended one is
  # terminated at once; MICROVM_MAX_SECONDS bounds its life in any case.
  run=$(aws lambda-microvms run-microvm --image-identifier "$image" \
    --execution-role-arn "$role" \
    --ingress-network-connectors "$MICROVM_CONNECTOR:ALL_INGRESS" \
    --idle-policy '{"autoResumeEnabled": false, "maxIdleDurationSeconds": 60, "suspendedDurationSeconds": 0}' \
    --maximum-duration-in-seconds "$MICROVM_MAX_SECONDS" \
    --query '[microvmId, endpoint]' --output text)
  read -r id endpoint <<< "$run"
  trap 'aws lambda-microvms terminate-microvm --microvm-identifier "$id" > /dev/null; rm -rf "$work"' EXIT
  # The token goes from the CLI's output into a header file that only this
  # user can read, and never onto a command line.
  (umask 077 && printf 'X-aws-proxy-auth: %s\n' "$(aws lambda-microvms create-microvm-auth-token \
    --microvm-identifier "$id" --expiration-in-minutes 15 --allowed-ports '[{"port": 8080}]' \
    --query 'authToken."X-aws-proxy-auth"' --output text)" > "$work/auth")
  # The endpoint takes traffic once the /run hook has answered.
  for attempt in $(seq 60); do
    if curl --silent --fail --max-time 120 --request POST --header "@$work/auth" \
      "https://$endpoint/trial" > "$work/trial.json"; then
      break
    fi
    if [ "$attempt" = 60 ]; then
      echo "MicroVM $id never answered its trial request" >&2
      exit 1
    fi
    sleep 1
  done
  answered=$(now)
  aws lambda-microvms terminate-microvm --microvm-identifier "$id" > /dev/null
  trap 'rm -rf "$work"' EXIT
  aws lambda-microvms get-microvm --microvm-identifier "$id" --output json > "$work/microvm.json"
  emit "$(jq -n --slurpfile microvm "$work/microvm.json" '{microvm: $microvm[0]}')"
}

"invoke_$candidate"
