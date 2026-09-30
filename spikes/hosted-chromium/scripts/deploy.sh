#!/usr/bin/env bash
# Deploys one candidate's package of the trial, as package.sh built it
# locally (ADR-0008 amendment, 2026-09-30). It refuses to run before
# budget.sh has created the budget alarm. Each candidate gets resources of its
# own, with the fixed names in common.sh and the tag aqa-spike=hosted-chromium;
# teardown.sh deletes them. A second deploy of the same candidate fails on its
# first create: run teardown.sh first.
#
# Roles hold the least the candidate needs: writing its own logs, and for
# Fargate and the MicroVM's build, reading its own image or zip. The trial's
# browser inherits the Lambda function's role, so that role can do nothing
# else.
#
# Usage: spikes/hosted-chromium/scripts/deploy.sh lambda|fargate|microvm
source "$(dirname "$0")/common.sh"

candidate=${1:-}
is_candidate "$candidate" || usage_candidate
require_budget

account=$(account_id)
if ! commit=$(docker image inspect "aqa-spike-trial:$candidate" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'); then
  echo "no local image aqa-spike-trial:$candidate: run package.sh $candidate first" >&2
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

# Prints the ARN of a new role that only `service` may assume, holding the one
# inline policy in the file `policy`. Lambda MicroVMs' roles must also allow
# sts:TagSession (microvms-security.html), so every role does.
create_role() {
  local role=$1 service=$2 policy=$3
  jq -n --arg service "$service" '{Version: "2012-10-17", Statement: [
      {Effect: "Allow", Principal: {Service: $service},
       Action: ["sts:AssumeRole", "sts:TagSession"]}]}' > "$work/trust-$role.json"
  aws iam create-role --role-name "$role" --path "$IAM_PATH" \
    --assume-role-policy-document "file://$work/trust-$role.json" \
    --tags "Key=$TAG_KEY,Value=$TAG_VALUE" --query Role.Arn --output text
  aws iam put-role-policy --role-name "$role" --policy-name "$role" \
    --policy-document "file://$policy"
}

# A new role takes seconds to reach the services that assume it.
# https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot_general.html#troubleshoot_general_eventual-consistency
settle_iam() {
  sleep 15
}

# Pushes the candidate's image to a repository of its own, and prints its URI.
push_image() {
  local repo=$NAME-$candidate registry=$account.dkr.ecr.$AWS_REGION.amazonaws.com
  aws ecr create-repository --repository-name "$repo" \
    --tags "Key=$TAG_KEY,Value=$TAG_VALUE" > /dev/null
  aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" > /dev/null
  docker tag "aqa-spike-trial:$candidate" "$registry/$repo:$commit"
  docker push --quiet "$registry/$repo:$commit" > /dev/null
  echo "$registry/$repo:$commit"
}

deploy_lambda() {
  local uri role_arn
  uri=$(push_image)
  create_log_group lambda
  policy "$(logs_statement lambda)" > "$work/policy.json"
  role_arn=$(create_role "$(role_name lambda)" lambda.amazonaws.com "$work/policy.json")
  settle_iam
  aws lambda create-function --function-name "$NAME" --package-type Image \
    --code "ImageUri=$uri" --role "$role_arn" --architectures arm64 \
    --memory-size "$MEMORY_MB" --timeout 120 \
    --logging-config "LogFormat=Text,LogGroup=$(log_group lambda)" \
    --tags "$TAG_KEY=$TAG_VALUE" > /dev/null
  aws lambda wait function-active-v2 --function-name "$NAME"
}

deploy_fargate() {
  local uri role_arn vpc repo_arn
  uri=$(push_image)
  create_log_group fargate
  repo_arn=arn:aws:ecr:$AWS_REGION:$account:repository/$NAME-fargate
  policy "$(logs_statement fargate)" \
    '{"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"}' \
    "$(jq -n --arg arn "$repo_arn" '{Effect: "Allow", Resource: $arn, Action: [
        "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]}')" \
    > "$work/policy.json"
  role_arn=$(create_role "$(role_name fargate)" ecs-tasks.amazonaws.com "$work/policy.json")
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
    --arg role "$role_arn" --arg image "$uri" --arg group "$(log_group fargate)" \
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
  local zip=$spike_dir/build/microvm-$commit.zip bucket key build_arn state
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
  policy "$(logs_statement microvm)" \
    "$(jq -n --arg arn "arn:aws:s3:::$bucket/$key" '{Effect: "Allow", Action: "s3:GetObject", Resource: $arn}')" \
    > "$work/build-policy.json"
  build_arn=$(create_role "$(role_name microvm-build)" lambda.amazonaws.com "$work/build-policy.json")
  # The role each MicroVM runs with, which invoke.sh passes: logs only.
  policy "$(logs_statement microvm)" > "$work/policy.json"
  create_role "$(role_name microvm)" lambda.amazonaws.com "$work/policy.json" > /dev/null
  settle_iam
  # Lambda snapshots the server once /ready answers, and a MicroVM takes
  # traffic once /run answers (microvms-images.html#microvms-images-build-hooks).
  # No egress connector: the trial reaches nothing outside its MicroVM.
  aws lambda-microvms create-microvm-image --name "$NAME" \
    --code-artifact "uri=s3://$bucket/$key" --base-image-arn "$MICROVM_BASE_IMAGE" \
    --build-role-arn "$build_arn" --cpu-configurations architecture=ARM_64 \
    --resources "minimumMemoryInMiB=$MEMORY_MB" \
    --hooks '{"port": 8080, "microvmHooks": {"run": "ENABLED", "runTimeoutInSeconds": 30},
              "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 300}}' \
    --logging "{\"cloudWatch\": {\"logGroup\": \"$(log_group microvm)\"}}" \
    --tags "$TAG_KEY=$TAG_VALUE" > /dev/null
  # No wait command exists for the build: poll it, for up to 30 minutes.
  for _ in $(seq 120); do
    state=$(aws lambda-microvms get-microvm-image --image-identifier "$NAME" \
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
