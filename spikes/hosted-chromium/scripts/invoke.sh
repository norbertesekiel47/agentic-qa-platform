#!/usr/bin/env bash
# One trial on a deployed candidate (ADR-0008 amendment, 2026-09-30), printed
# as one line of JSON: the trial's report, when the request went out and when
# the answer came back (this machine's clock), and the platform's own record of
# the run. The README's measurement plan says what cold and warm mean for each
# candidate, and what each field measures. A Fargate task has no warm start.
#
# Usage: spikes/hosted-chromium/scripts/invoke.sh lambda|fargate|microvm cold|warm
source "$(dirname "$0")/common.sh"
require_tools curl

candidate=${1:-}
start=${2:-}
is_candidate "$candidate" || usage_candidate "cold|warm"
case $start in cold | warm) ;; *) usage_candidate "cold|warm" ;; esac
require_budget

# What must not outlive the script, whatever stops it: a task still running
# and a MicroVM not yet terminated.
task=""
microvm=""
cleanup() {
  if [ -n "$task" ]; then
    aws ecs stop-task --cluster "$NAME" --task "$task" > /dev/null
  fi
  if [ -n "$microvm" ]; then
    aws lambda-microvms terminate-microvm --microvm-identifier "$microvm" > /dev/null
  fi
  rm -rf "$work"
}

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
  local stream report
  if [ "$start" = cold ]; then
    aws lambda update-function-configuration --function-name "$NAME" \
      --environment "Variables={AQA_SPIKE_START=$(now)}" > /dev/null
    aws lambda wait function-updated-v2 --function-name "$NAME"
  fi
  requested=$(now)
  aws lambda invoke --function-name "$NAME" --payload '{}' \
    --cli-binary-format raw-in-base64-out --log-type Tail --cli-read-timeout 150 \
    --output json "$work/answer.json" > "$work/invoke.json"
  answered=$(now)
  if jq -e 'has("FunctionError")' "$work/invoke.json" > /dev/null; then
    echo "the trial failed on Lambda:" >&2
    cat "$work/answer.json" >&2
    exit 1
  fi
  jq .trial "$work/answer.json" > "$work/trial.json"
  stream=$(jq -r .log_stream "$work/answer.json")
  # The log's tail ends with Lambda's REPORT line: durations and memory used.
  jq -r .LogResult "$work/invoke.json" | python3 -m base64 -d > "$work/log.txt"
  report=$(grep '^REPORT' "$work/log.txt")
  emit "$(jq -n --arg report "$report" --arg stream "$stream" \
    '{report: $report, log_stream: $stream}')"
}

invoke_fargate() {
  if [ "$start" = warm ]; then
    echo "every Fargate task starts cold: there is no warm start to measure" >&2
    exit 2
  fi
  local subnets group arn
  subnets=$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true \
    --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
  group=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" \
    --query 'SecurityGroups[].GroupId' --output text)
  requested=$(now)
  task=$(aws ecs run-task --cluster "$NAME" --launch-type FARGATE --task-definition "$NAME" \
    --propagate-tags TASK_DEFINITION \
    --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$group],assignPublicIp=ENABLED}" \
    --query 'tasks[0].taskArn' --output text)
  aws ecs wait tasks-stopped --cluster "$NAME" --tasks "$task"
  answered=$(now)
  arn=$task
  task=""
  aws ecs describe-tasks --cluster "$NAME" --tasks "$arn" --query 'tasks[0]' \
    --output json > "$work/task.json"
  if ! jq -e '.containers[0].exitCode == 0' "$work/task.json" > /dev/null; then
    echo "the trial's task failed:" >&2
    jq . "$work/task.json" >&2
    exit 1
  fi
  # The trial prints its report as one line of JSON, which can reach the log a
  # few seconds after the task stops; Chromium may log other lines around it.
  for attempt in $(seq 30); do
    aws logs get-log-events --log-group-name "$(log_group fargate)" \
      --log-stream-name "trial/trial/${arn##*/}" --start-from-head \
      --query 'events[]' --output json > "$work/events.json"
    jq -c '[.[] | select(.message | startswith("{"))] | last' "$work/events.json" \
      > "$work/event.json"
    if [ "$(cat "$work/event.json")" != null ]; then
      break
    fi
    if [ "$attempt" = 30 ]; then
      echo "the task's log holds no report: see $(log_group fargate)" >&2
      exit 1
    fi
    sleep 1
  done
  jq '.message | fromjson' "$work/event.json" > "$work/trial.json"
  emit "$(jq -n --slurpfile task "$work/task.json" --slurpfile event "$work/event.json" \
    '{task: $task[0], reported_at: ($event[0].timestamp / 1000)}')"
}

invoke_microvm() {
  local image role run endpoint token
  image=$(aws lambda-microvms list-microvm-images --output text \
    --query "items[?name=='$NAME'].imageArn | [0]")
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
  read -r microvm endpoint <<< "$run"
  # The token goes from the CLI's output into a header file that only this
  # user can read, and never onto a command line.
  token=$(aws lambda-microvms create-microvm-auth-token --microvm-identifier "$microvm" \
    --expiration-in-minutes 15 --allowed-ports '[{"port": 8080}]' \
    --query 'authToken."X-aws-proxy-auth"' --output text)
  (umask 077 && printf 'X-aws-proxy-auth: %s\n' "$token" > "$work/auth")
  # Lambda routes traffic to the server once its /run hook has answered.
  for attempt in $(seq 60); do
    if curl --silent --fail --max-time 10 --header "@$work/auth" \
      "https://$endpoint/health" > /dev/null; then
      break
    fi
    if [ "$attempt" = 60 ]; then
      echo "MicroVM $microvm never answered" >&2
      exit 1
    fi
    sleep 1
  done
  # One request, never repeated: a second trial on this MicroVM would read as
  # an environment used twice.
  curl --silent --fail --max-time 120 --request POST --header "@$work/auth" \
    "https://$endpoint/trial" > "$work/trial.json"
  answered=$(now)
  local id=$microvm
  aws lambda-microvms terminate-microvm --microvm-identifier "$id" > /dev/null
  microvm=""
  aws lambda-microvms get-microvm --microvm-identifier "$id" --output json \
    > "$work/microvm.json"
  emit "$(jq -n --slurpfile microvm "$work/microvm.json" '{microvm: $microvm[0]}')"
}

"invoke_$candidate"
