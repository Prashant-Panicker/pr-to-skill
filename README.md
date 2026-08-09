# pr-review-profiler

Mines a person's historical PR feedback into a reusable `SKILL.md`, then uses
that history as repository-scoped evidence when new pull requests are opened.
The batch and event-driven paths share provider-neutral application ports.

Azure Foundry is retained for model and embedding calls because it is the
approved organizational AI interface. All other deployed infrastructure is
AWS: API Gateway, Lambda, SQS, S3, DynamoDB, Secrets Manager, and OpenSearch
Serverless.

## Architecture

```text
GitHub webhook
  -> API Gateway -> webhook Lambda (raw-body HMAC verification)
  -> review SQS -> review Lambda
       pull_request opened/reopened/synchronize/ready_for_review
         -> retrieve repository-scoped OpenSearch evidence
         -> show Azure Foundry the PR diff and immutable head file tree
         -> fetch only the branch files Azure Foundry requests for context
         -> generate a review with Azure Foundry
         -> save it in S3 and post a GitHub review for the verified head SHA
  -> mining SQS -> mining Lambda
       pull_request_review submitted/edited/dismissed
       pull_request_review_comment created/edited/deleted
      issue_comment created/edited/deleted on a pull request
       pull_request closed
         -> remember open PRs, but mine only after merge
         -> select trusted-reviewer root comments and include all thread replies
         -> verify requested changes against the final merged diff
         -> let Azure Foundry select durable guidance and examples
         -> replace/delete notes by stable GitHub ID
         -> delete older architecture notes when an accepted change supersedes them
         -> update OpenSearch vectors
         -> regenerate notes.json and SKILL.md in S3
```

`application_ports.py` owns the interfaces used by the workflow. AWS SDK types
are confined to `aws_adapters.py`, `aws_runtime.py`, and `lambda_handler.py`;
Azure SDK types are confined to the Foundry client and authentication adapter.

Completed GitHub delivery IDs and expiring workflow locks are stored in
DynamoDB. Each SQS queue retries failed deliveries and sends a message to the
DLQ after five receives. Posted reviews carry a hidden head-SHA marker, so a
retry does not post the same review twice.

PR review uses a bounded two-stage context flow and never clones the repository.
The first model call receives the PR description, changed-file patches,
historical evidence, and file paths plus sizes from the PR's immutable head
tree. It may request up to eight exact paths. Only those UTF-8 blobs are then
fetched by Git SHA and supplied to the final review call, allowing the review to
inspect surrounding implementations, callers, configuration, and existing
tests when needed. Each selected file is limited to 50,000 bytes, selected
context to 120,000 characters, and the repository manifest to 20,000 files or
200,000 characters. Invalid, duplicate, oversized, or non-tree paths fail
explicitly rather than broadening repository access. Oversized files are not
offered for selection, and selected blobs that are not UTF-8 text are skipped.
Selective tree/blob API calls use a 15-second timeout without retries. The
deployed review worker caps each Azure embedding or generation call at 120
seconds and disables SDK retries so the workflow remains bounded by its Lambda
deadline.
Each model input is serialized as JSON and limited to 400,000 characters.

## Local setup

Use Python 3.12, matching the Lambda runtime:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
set -a; source .env; set +a
gh auth login
```

Set `person.github_usernames` and `repos` in the git-ignored `config.yaml`.
Only root feedback from those users is eligible for mining; replies in their
threads may be from any GitHub user. Unmerged and AI-rejected guidance remains
out of both the vector index and generated skill.
Implementation checks use GitHub's complete merged diff representation, not
the sometimes-omitted per-file `patch` fields. Diffs above 500,000 characters
fail explicitly instead of being silently truncated.
Historical collection uses the authenticated `gh` CLI. Azure Foundry accepts
either `AZURE_OPENAI_API_KEY` or `DefaultAzureCredential` (`az login`) locally.
The application does not load `.env` automatically. Leave
`AWS_OPENSEARCH_ENABLED=false` for mining and skill generation without vector
storage; enable it only after configuring an AWS profile and collection.

Run the batch pipeline:

```bash
python main.py --config config.yaml
python main.py --config config.yaml --skip-collect
python main.py --config config.yaml --skip-analyze
```

## Local AWS resources

The supported AWS dependencies can run in LocalStack while model and embedding
calls continue to use your real Azure Foundry deployment. This path is useful
while AWS account provisioning is pending. It follows the approved local
convention of Podman, LocalStack 4.1.0, loopback-only ports, region
`us-east-1`, and non-secret `test` credentials.

Start Podman and LocalStack:

```bash
podman machine start
podman pull centraluhg.jfrog.io/glb-docker-hub-rem-cache/localstack/localstack:4.1.0
make local-up
curl --fail --silent --show-error http://localhost:4566/_localstack/health
```

If the JFrog cache does not contain the pinned image, use the official image
for that invocation:

```bash
podman pull docker.io/localstack/localstack:4.1.0
LOCALSTACK_IMAGE=docker.io/localstack/localstack:4.1.0 make local-up
```

Load your existing ignored `.env` first. The bootstrap stores any configured
`GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_PRIVATE_KEY`, and
`AZURE_OPENAI_API_KEY` directly in LocalStack Secrets Manager; it does not
write secret values to the generated file. An Azure key is optional when your
Foundry deployment accepts the active `az login` identity.

```bash
set -a; source .env; set +a
make local-bootstrap
set -a; source .env.localstack; set +a
```

The default bootstrap creates an S3 artifact bucket, DynamoDB delivery table,
two SQS work queues and their dead-letter queue, and optional Secrets Manager
values. It deliberately leaves OpenSearch disabled because the local engine is
resource intensive. Batch mining, skill generation, and S3 synchronization do
not require the vector index.

Run the batch pipeline and copy its canonical outputs to local S3 while
continuing to use Azure Foundry:

```bash
python main.py --config config.yaml --sync-aws-artifacts
```

Local OpenSearch is an explicit, resource-intensive opt-in. It substitutes a
regular OpenSearch domain for AOSS and does not prove Serverless behavior. Only
enable it on a machine with sufficient memory and after stopping other heavy
workloads:

```bash
LOCALSTACK_SERVICES=s3,sqs,dynamodb,secretsmanager,opensearch make local-up
PR_TO_SKILL_LOCAL_OPENSEARCH=true make local-bootstrap
```

Stop LocalStack without removing its named volume:

```bash
make local-down
```

This setup currently runs the batch process and AWS adapters in the host Python
process. It provisions queues for adapter and event-flow testing, but does not
deploy the SAM Lambda/API Gateway resources. That deployment requires SAM CLI
packaging plus a local template that substitutes a regular OpenSearch domain
for the production AOSS collection. LocalStack also does not prove production
IAM enforcement, networking, scaling, quotas, X-Ray behavior, or exact AOSS
semantics; those remain AWS sandbox checks.

Enable `aws_opensearch` in the config and use an AWS profile with OpenSearch
data access to index and retrieve review notes:

```bash
python main.py --config config.yaml --search "missing authorization check"
```

A full batch run assesses every merged PR for architecture decisions, applies
merge-ordered supersession, upserts the complete selected set, and deletes
stale vectors. This converges bootstrap state with incremental mining.

Before enabling webhooks for an existing history set, bootstrap the canonical
artifacts into the deployed S3 bucket:

```bash
export ARTIFACT_BUCKET="$(aws cloudformation describe-stacks \
  --stack-name pr-to-skill-stage \
  --query "Stacks[0].Outputs[?OutputKey=='ArtifactBucketName'].OutputValue" \
  --output text)"
python main.py --config config.yaml --skip-collect --skip-analyze \
  --sync-aws-artifacts
```

This prevents the first feedback event from rebuilding `SKILL.md` from only
one pull request.

## AWS deployment

The SAM template provisions the complete single-repository demo. The workflow
at `.github/workflows/deploy-aws.yml` runs tests, assumes an AWS role through
GitHub OIDC, synchronizes runtime secrets into Secrets Manager, validates and
builds the template, and deploys it. Push to `main` or dispatch it manually.
The SAM Makefile build allowlists Python source and `config.example.yaml`, so
local `config.yaml`, output artifacts, and tests are not included in Lambda.

Create a GitHub Environment named **`stage`**. Add protection rules as needed,
then configure the following Environment variables and secrets.

### GitHub Environment variables

| Variable | Required | Example / purpose |
| --- | --- | --- |
| `AWS_REGION` | Yes | `us-east-1` |
| `AWS_DEPLOY_ROLE_ARN` | Yes | OIDC role assumed by GitHub Actions |
| `AWS_STACK_NAME` | Yes | `pr-to-skill-stage` |
| `AWS_SAM_S3_PREFIX` | No | `pr-to-skill/stage`; defaults to this value |
| `PR_TO_SKILL_REPO` | Yes | One repository, such as `org/repo` |
| `PR_TO_SKILL_REVIEWERS` | Yes | Comma-separated trusted GitHub logins, such as `alice,bob` |
| `GH_APP_ID` | Yes | GitHub App ID |
| `GH_INSTALLATION_ID` | Yes | App installation ID for the repository |
| `GH_WEBHOOK_SECRET_ID` | Yes | Secrets Manager name, such as `pr-to-skill/stage/github-webhook` |
| `GH_APP_PRIVATE_KEY_SECRET_ID` | Yes | Secrets Manager name for the PEM key |
| `AZURE_OPENAI_API_KEY_SECRET_ID` | Yes | Secrets Manager name for the Foundry key |
| `AZURE_OPENAI_ENDPOINT` | Yes | Azure Foundry/OpenAI endpoint |
| `AZURE_OPENAI_DEPLOYMENT` | Yes | Chat model deployment name |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | Yes | Embedding deployment name |
| `AZURE_OPENAI_API_VERSION` | Yes | `2025-03-01-preview` |
| `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | Yes | Must match the embedding deployment, commonly `1536` |

Secret names are variables, not secrets. The template passes names to Lambda
and derives scoped IAM ARNs for only those Secrets Manager resources. Use names,
not ARNs, because the workflow creates a missing secret by name.

### GitHub Environment secrets

| Secret | Required | Destination |
| --- | --- | --- |
| `GH_WEBHOOK_SECRET` | Yes | `GH_WEBHOOK_SECRET_ID` |
| `GH_APP_PRIVATE_KEY` | Yes | `GH_APP_PRIVATE_KEY_SECRET_ID` |
| `AZURE_OPENAI_API_KEY` | Yes | `AZURE_OPENAI_API_KEY_SECRET_ID` |

No long-lived AWS access key is needed. The deployment role trust policy must
allow the repository's `stage` environment subject:
`repo:<owner>/<repo>:environment:stage`. It needs CloudFormation, SAM artifact
S3, IAM role, Lambda, API Gateway, SQS, DynamoDB, S3, Secrets Manager,
OpenSearch Serverless, and `iam:PassRole` permissions required by the template.
The workflow additionally needs permission to create/update the three named
Secrets Manager secrets.

After deployment, the workflow prints the `WebhookUrl` stack output. Configure
that URL in the GitHub App, set the same webhook secret, and subscribe to:

- Pull requests
- Pull request reviews
- Pull request review comments
- Issue comments

The GitHub App needs Metadata read, Contents read, Issues read, and Pull requests
**write**. Issues read supplies PR conversation comments; Pull requests write
allows the review worker to post generated reviews.

## Runtime environment

SAM supplies AWS resource addresses and deployment settings to Lambda. These
are documented for troubleshooting and non-SAM hosts; do not set them as
GitHub Environment values unless replacing the template's wiring.

| Runtime variable | Owner / meaning |
| --- | --- |
| `ARTIFACT_BUCKET`, `ARTIFACT_PREFIX` | S3 artifact location; prefix defaults to `artifacts` |
| `DELIVERY_TABLE` | DynamoDB receipt and lock table |
| `REVIEW_QUEUE_URL`, `MINING_QUEUE_URL` | SQS URLs used by webhook Lambda |
| `PR_TO_SKILL_REVIEWERS` | Comma-separated trusted reviewer logins |
| `AWS_OPENSEARCH_ENABLED` | Must be `true` in the worker |
| `AWS_OPENSEARCH_ENDPOINT` | OpenSearch Serverless collection endpoint |
| `AWS_OPENSEARCH_INDEX` | Defaults to `pr-review-notes` |
| `AWS_OPENSEARCH_SERVICE` | `aoss` for Serverless |
| `AWS_OPENSEARCH_SIGN_REQUESTS` | Defaults to `true`; use `false` for unsigned local OpenSearch |
| `AWS_OPENSEARCH_VERIFY_CERTS` | Defaults to `true`; use `false` only for local HTTP/testing |
| `PR_TO_SKILL_AWS_ENDPOINT_URL` | Optional boto3 endpoint override; use `http://localhost:4566` for host-run LocalStack |
| `AWS_REGION` | Lambda-provided AWS region |
| `PR_TO_SKILL_CONFIG` | Packaged YAML path, defaults to `config.example.yaml` |
| `PR_TO_SKILL_OPENAI_TIMEOUT` | Per-request cap, maximum `600` seconds |
| `PR_TO_SKILL_WORKFLOW_DEADLINE` | Workflow cap, maximum `780` seconds |
| `AZURE_OPENAI_API_MODE` | Optional; defaults from YAML to `responses` |
| `AGENT_MAX_OUTPUT_TOKENS` | Optional model output override |

## Output

- `output/raw_comments.json`: normalized historical feedback.
- `output/merged_pull_requests.json`: merged PR metadata and complete diffs.
- `output/notes.json` and `output/notes.md`: AI-annotated review guidance.
- `output/SKILL.md`: consolidated review skill.
- `s3://<artifact-bucket>/artifacts/reviews/pr-<number>.md`: generated review.
- Canonical webhook history artifacts live under the same S3 prefix.

## Limitations

- The deployed demo supports exactly one repository and multiple trusted reviewers.
- Each worker's reserved concurrency is one. Cross-service S3, DynamoDB, OpenSearch,
  and GitHub operations are not a transaction; production scale-out needs a
  stronger workflow/fencing model.
- The OpenSearch Serverless network policy allows the public collection
  endpoint, but IAM and the data access policy still restrict data operations
  to the worker role. Use a VPC endpoint and private network policy where the
  organization requires private connectivity.
- One PR is limited to 100 feedback items by default, and one workflow is
  bounded to fit within Lambda's 15-minute timeout.
- Analysis responses are schema-validated and retried. A persistently invalid
  batch fails instead of silently creating a partial skill.