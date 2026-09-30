#!/usr/bin/env bash
# Deploys one candidate's package of the trial, as package.sh built it
# locally (ADR-0008 amendment, 2026-09-30). It refuses to run before
# budget.sh has created the budget alarm, and refuses a candidate that is
# already deployed. Each candidate gets resources of its own, with the fixed
# names in common.sh; teardown.sh deletes them.
#
# Roles hold the least the candidate needs: writing its own logs, and for
# Fargate and the MicroVM's build, reading its own image or zip. The trial's
# browser inherits the Lambda function's role, so that role can do nothing
# else.
#
# Usage: spikes/hosted-chromium/scripts/deploy.sh lambda|fargate|microvm
source "$(dirname "$0")/common.sh"
require_tools docker

candidate=${1:-}
is_candidate "$candidate" || usage_candidate
require_budget

account=$(account_id)
# A deploy creates its candidate's log group, so an existing one means the
# candidate is deployed, fully or partly.
deployed=$(aws logs describe-log-groups --log-group-name-prefix "$(log_group "$candidate")" \
  --query 'logGroups[].logGroupName' --output text)
if [ -n "$deployed" ]; then
  echo "$candidate is already deployed: run teardown.sh first" >&2
  exit 1
fi
commit=$(docker image inspect "aqa-spike-trial:$candidate" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
if [[ ! $commit =~ ^[0-9a-f]{12}$ ]]; then
  echo "aqa-spike-trial:$candidate names no commit: build it with package.sh $candidate" >&2
  exit 1
fi

logs_statement() {
  jq -n --arg arn "arn:aws:logs:$AWS_REGION:$account:log-group:$(log_group "$1"):*" \
    '{Effect: "Allow", Action: ["logs:CreateLogStream", "logs:PutLogEvents"], Resource: $arn}'
}

# A policy document from statements given as JSON arguments.
policy() {
  jq -n '{Version: "2012-10-17", Statement: $ARGS.positional}' --jsonargs "$@"
}

create_log_group() {
  aws logs create-log-group --log-group-name "$(log_group "$1")" \
    --tags "$TAG_KEY=$TAG_VALUE"
  aws logs put-retention-policy --log-group-name "$(log_group "$1")" \
    --retention-in-days 7
}

# Creates a role that only `service` may assume, holding the one inline policy
# in the file `policy`, and sets role_arn. With `tag_session`, the role may
# also tag its session, which Lambda MicroVMs' roles must (microvms-security.html).
create_role() {
  local role=$1 service=$2 policy=$3 actions='["sts:AssumeRole"]'
  if [ "${4:-}" = tag_session ]; then
    actions='["sts:AssumeRole", "sts:TagSession"]'
  fi
  jq -n --arg service "$service" --argjson actions "$actions" '{Version: "2012-10-17",
      Statement: [{Effect: "Allow", Principal: {Service: $service}, Action: $actions}]}' \
    > "$work/trust-$role.json"
  role_arn=$(aws iam create-role --role-name "$role" --path "$IAM_PATH" \
    --assume-role-policy-document "file://$work/trust-$role.json" \
    --tags "Key=$TAG_KEY,Value=$TAG_VALUE" --query Role.Arn --output text)
  aws iam put-role-policy --role-name "$role" --policy-name "$role" \
    --policy-document "file://$policy"
}

# A new role takes seconds to reach the services that assume it.
# https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot_general.html#troubleshoot_general_eventual-consistency
settle_iam() {
  sleep 15
}

# Pushes the candidate's image to a repository of its own, and sets image_uri.
push_image() {
  local repo=$NAME-$candidate registry=$account.dkr.ecr.$AWS_REGION.amazonaws.com
  aws ecr create-repository --repository-name "$repo" \
    --tags "Key=$TAG_KEY,Value=$TAG_VALUE" > /dev/null
  aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" > /dev/null
  image_uri=$registry/$repo:$commit
  docker tag "aqa-spike-trial:$candidate" "$image_uri"
  docker push --quiet "$image_uri" > /dev/null
}

deploy_lambda() {
  local logs
  push_image
  create_log_group lambda
  logs=$(logs_statement lambda)
  policy "$logs" > "$work/policy.json"
  create_role "$(role_name lambda)" lambda.amazonaws.com "$work/policy.json"
  settle_iam
  aws lambda create-function --function-name "$NAME" --package-type Image \
    --code "ImageUri=$image_uri" --role "$role_arn" --architectures arm64 \
    --memory-size "$MEMORY_MB" --timeout 120 \
    --logging-config "LogFormat=Text,LogGroup=$(log_group lambda)" \
    --tags "$TAG_KEY=$TAG_VALUE" > /dev/null
  aws lambda wait function-active-v2 --function-name "$NAME"
}

deploy_fargate() {
  local logs pull vpc
  push_image
  create_log_group fargate
  logs=$(logs_statement fargate)
  pull=$(jq -n --arg arn "arn:aws:ecr:$AWS_REGION:$account:repository/$NAME-fargate" \
    '{Effect: "Allow", Resource: $arn, Action: ["ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]}')
  policy "$logs" "$pull" \
    '{"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"}' \
    > "$work/policy.json"
  create_role "$(role_name fargate)" ecs-tasks.amazonaws.com "$work/policy.json"
  aws ecs create-cluster --cluster-name "$NAME" \
    --tags "key=$TAG_KEY,value=$TAG_VALUE" > /dev/null
  vpc=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true \
    --query 'Vpcs[0].VpcId' --output text)
  if [ -z "$vpc" ] || [ "$vpc" = None ]; then
    echo "no default VPC in $AWS_REGION for the task" >&2
    exit 1
  fi
  # No inbound rule: the task only reaches out, to pull its image and to log.
  aws ec2 create-security-group --group-name "$NAME" --vpc-id "$vpc" \
    --description "The hosted-compute spike's Fargate task: no inbound traffic" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=$TAG_KEY,Value=$TAG_VALUE}]" \
    > /dev/null
  # The task gets no task role, so its container holds no AWS credentials.
  jq -n --arg family "$NAME" --arg cpu "$FARGATE_CPU" --arg memory "$MEMORY_MB" \
    --arg role "$role_arn" --arg image "$image_uri" --arg group "$(log_group fargate)" \
    --arg region "$AWS_REGION" --arg key "$TAG_KEY" --arg value "$TAG_VALUE" '{
      family: $family, requiresCompatibilities: ["FARGATE"], networkMode: "awsvpc",
      cpu: $cpu, memory: $memory, executionRoleArn: $role,
      runtimePlatform: {cpuArchitecture: "ARM64", operatingSystemFamily: "LINUX"},
      containerDefinitions: [{name: "trial", image: $image, essential: true,
        logConfiguration: {logDriver: "awslogs", options: {
          "awslogs-group": $group, "awslogs-region": $region,
          "awslogs-stream-prefix": "trial"}}}],
      tags: [{key: $key, value: $value}]}' > "$work/task.json"
  settle_iam
  aws ecs register-task-definition --cli-input-json "file://$work/task.json" > /dev/null
}

deploy_microvm() {
  local zip=$spike_dir/build/microvm-$commit.zip bucket key logs get_zip build_role image state
  if [ ! -f "$zip" ]; then
    echo "no $zip: run package.sh microvm first" >&2
    exit 1
  fi
  bucket=$(bucket_name)
  key=microvm-$commit.zip
  aws s3api create-bucket --bucket "$bucket" > /dev/null
  aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-tagging --bucket "$bucket" \
    --tagging "TagSet=[{Key=$TAG_KEY,Value=$TAG_VALUE}]"
  aws s3 cp --only-show-errors "$zip" "s3://$bucket/$key"
  create_log_group microvm
  logs=$(logs_statement microvm)
  get_zip=$(jq -n --arg arn "arn:aws:s3:::$bucket/$key" \
    '{Effect: "Allow", Action: "s3:GetObject", Resource: $arn}')
  policy "$logs" "$get_zip" > "$work/build-policy.json"
  create_role "$(role_name microvm-build)" lambda.amazonaws.com "$work/build-policy.json" tag_session
  build_role=$role_arn
  # The role each MicroVM runs with, which invoke.sh passes: logs only.
  policy "$logs" > "$work/policy.json"
  create_role "$(role_name microvm)" lambda.amazonaws.com "$work/policy.json" tag_session
  settle_iam
  # Lambda snapshots the server once /ready answers, and a MicroVM takes
  # traffic once /run answers (microvms-images.html#microvms-images-build-hooks).
  # No egress connector: the trial reaches nothing outside its MicroVM.
  image=$(aws lambda-microvms create-microvm-image --name "$NAME" \
    --code-artifact "uri=s3://$bucket/$key" --base-image-arn "$MICROVM_BASE_IMAGE" \
    --build-role-arn "$build_role" --cpu-configurations architecture=ARM_64 \
    --resources "minimumMemoryInMiB=$MEMORY_MB" \
    --hooks '{"port": 8080, "microvmHooks": {"run": "ENABLED", "runTimeoutInSeconds": 30},
              "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 300}}' \
    --logging "{\"cloudWatch\": {\"logGroup\": \"$(log_group microvm)\"}}" \
    --tags "$TAG_KEY=$TAG_VALUE" --query imageArn --output text)
  # No wait command exists for the build: poll it, for up to 30 minutes.
  for _ in $(seq 120); do
    state=$(aws lambda-microvms get-microvm-image --image-identifier "$image" \
      --query state --output text)
    case $state in
      CREATED) return 0 ;;
      *FAILED)
        echo "the MicroVM image build ended $state: see $(log_group microvm)" >&2
        exit 1
        ;;
    esac
    sleep 15
  done
  echo "the MicroVM image build didn't finish in 30 minutes" >&2
  exit 1
}

"deploy_$candidate"
echo "deployed $candidate at $commit"
