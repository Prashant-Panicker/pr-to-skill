#!/usr/bin/env bash
set -euo pipefail

endpoint="${PR_TO_SKILL_AWS_ENDPOINT_URL:-http://localhost:4566}"
region="${AWS_REGION:-us-east-1}"
stage="${PR_TO_SKILL_LOCAL_STAGE:-local}"
prefix="pr-to-skill-${stage}"
bucket="${prefix}-artifacts"
table="${prefix}-delivery"
dead_letter_queue="${prefix}-dead-letter"
review_queue="${prefix}-review"
mining_queue="${prefix}-mining"
domain="${prefix}"
environment_file=".env.localstack"
enable_opensearch="${PR_TO_SKILL_LOCAL_OPENSEARCH:-false}"

if [[ "$enable_opensearch" != "true" && "$enable_opensearch" != "false" ]]; then
  echo "PR_TO_SKILL_LOCAL_OPENSEARCH must be true or false" >&2
  exit 1
fi

export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="$region"

aws_local() {
  aws --endpoint-url "$endpoint" --region "$region" "$@"
}

write_setting() {
  printf '%s=%q\n' "$1" "$2" >>"$environment_file"
}

if ! curl --fail --silent --show-error "$endpoint/_localstack/health" >/dev/null; then
  echo "LocalStack is not healthy at $endpoint" >&2
  exit 1
fi

if ! aws_local s3api head-bucket --bucket "$bucket" 2>/dev/null; then
  if [[ "$region" == "us-east-1" ]]; then
    aws_local s3api create-bucket --bucket "$bucket" >/dev/null
  else
    aws_local s3api create-bucket \
      --bucket "$bucket" \
      --create-bucket-configuration "LocationConstraint=$region" >/dev/null
  fi
fi

if ! aws_local dynamodb describe-table --table-name "$table" >/dev/null 2>&1; then
  aws_local dynamodb create-table \
    --table-name "$table" \
    --billing-mode PAY_PER_REQUEST \
    --attribute-definitions AttributeName=key,AttributeType=S \
    --key-schema AttributeName=key,KeyType=HASH >/dev/null
  aws_local dynamodb wait table-exists --table-name "$table"
fi

dead_letter_url="$(aws_local sqs create-queue \
  --queue-name "$dead_letter_queue" \
  --attributes MessageRetentionPeriod=1209600 \
  --query QueueUrl --output text)"
dead_letter_arn="$(aws_local sqs get-queue-attributes \
  --queue-url "$dead_letter_url" \
  --attribute-names QueueArn \
  --query Attributes.QueueArn --output text)"
redrive_policy="{\"deadLetterTargetArn\":\"${dead_letter_arn}\",\"maxReceiveCount\":\"5\"}"

create_work_queue() {
  local queue_name="$1"
  local queue_url
  queue_url="$(aws_local sqs create-queue \
    --queue-name "$queue_name" \
    --attributes VisibilityTimeout=5400 \
    --query QueueUrl --output text)"
  aws_local sqs set-queue-attributes \
    --queue-url "$queue_url" \
    --attributes "RedrivePolicy=${redrive_policy}" >/dev/null
  printf '%s' "$queue_url"
}

review_queue_url="$(create_work_queue "$review_queue")"
mining_queue_url="$(create_work_queue "$mining_queue")"

put_secret() {
  local secret_name="$1"
  local environment_name="$2"
  local secret_value="${!environment_name:-}"
  if [[ -z "$secret_value" ]]; then
    echo "Skipping $secret_name because $environment_name is not set" >&2
    return
  fi
  if aws_local secretsmanager describe-secret --secret-id "$secret_name" >/dev/null 2>&1; then
    printf '%s' "$secret_value" | aws_local secretsmanager put-secret-value \
      --secret-id "$secret_name" --secret-string file:///dev/stdin >/dev/null
  else
    printf '%s' "$secret_value" | aws_local secretsmanager create-secret \
      --name "$secret_name" --secret-string file:///dev/stdin >/dev/null
  fi
}

webhook_secret_id="${prefix}/github-webhook"
github_key_secret_id="${prefix}/github-app-private-key"
azure_key_secret_id="${prefix}/azure-openai-api-key"
put_secret "$webhook_secret_id" GITHUB_WEBHOOK_SECRET
put_secret "$github_key_secret_id" GITHUB_APP_PRIVATE_KEY
put_secret "$azure_key_secret_id" AZURE_OPENAI_API_KEY

opensearch_endpoint=""
if [[ "$enable_opensearch" == "true" ]]; then
  if ! aws_local opensearch describe-domain --domain-name "$domain" >/dev/null 2>&1; then
    aws_local opensearch create-domain \
      --domain-name "$domain" \
      --engine-version OpenSearch_2.11 >/dev/null
  fi
  domain_ready="false"
  for _ in {1..120}; do
    processing="$(aws_local opensearch describe-domain \
    --domain-name "$domain" \
      --query DomainStatus.Processing --output text)"
    if [[ "$processing" == "False" || "$processing" == "false" ]]; then
      domain_ready="true"
      break
    fi
    sleep 2
  done
  if [[ "$domain_ready" != "true" ]]; then
    echo "OpenSearch domain $domain did not become ready within four minutes" >&2
    exit 1
  fi
  opensearch_endpoint="$(aws_local opensearch describe-domain \
    --domain-name "$domain" \
    --query DomainStatus.Endpoint --output text)"
fi

: >"$environment_file"
write_setting AWS_ACCESS_KEY_ID test
write_setting AWS_SECRET_ACCESS_KEY test
write_setting AWS_REGION "$region"
write_setting PR_TO_SKILL_AWS_ENDPOINT_URL "$endpoint"
write_setting ARTIFACT_BUCKET "$bucket"
write_setting ARTIFACT_PREFIX artifacts
write_setting DELIVERY_TABLE "$table"
write_setting REVIEW_QUEUE_URL "$review_queue_url"
write_setting MINING_QUEUE_URL "$mining_queue_url"
write_setting AWS_OPENSEARCH_ENABLED "$enable_opensearch"
if [[ "$enable_opensearch" == "true" ]]; then
  write_setting AWS_OPENSEARCH_ENDPOINT "http://$opensearch_endpoint"
  write_setting AWS_OPENSEARCH_INDEX pr-review-notes
  write_setting AWS_OPENSEARCH_SERVICE es
  write_setting AWS_OPENSEARCH_SIGN_REQUESTS false
  write_setting AWS_OPENSEARCH_VERIFY_CERTS false
fi

if [[ -n "${GITHUB_WEBHOOK_SECRET:-}" ]]; then
  write_setting GITHUB_WEBHOOK_SECRET_ID "$webhook_secret_id"
fi
if [[ -n "${GITHUB_APP_PRIVATE_KEY:-}" ]]; then
  write_setting GITHUB_APP_PRIVATE_KEY_SECRET_ID "$github_key_secret_id"
fi
if [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
  write_setting AZURE_OPENAI_API_KEY_SECRET_ID "$azure_key_secret_id"
fi

echo "Local resources are ready. Load non-secret settings with:"
echo "  set -a; source $environment_file; set +a"