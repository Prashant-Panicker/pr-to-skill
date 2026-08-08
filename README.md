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
  -> SQS -> worker Lambda
       pull_request opened/reopened/synchronize/ready_for_review
         -> retrieve repository-scoped OpenSearch evidence
         -> generate an advisory review with Azure Foundry
         -> save reviews/pr-<number>.md in S3
       pull_request_review submitted/edited/dismissed
       pull_request_review_comment created/edited/deleted
         -> refetch the PR's complete feedback
         -> replace/delete notes by stable GitHub ID
         -> update OpenSearch vectors
         -> regenerate notes.json and SKILL.md in S3
```

`application_ports.py` owns the interfaces used by the workflow. AWS SDK types
are confined to `aws_adapters.py`, `aws_runtime.py`, and `lambda_handler.py`;
Azure SDK types are confined to the Foundry client and authentication adapter.

Completed GitHub delivery IDs and expiring workflow locks are stored in
DynamoDB. SQS retries failed deliveries and sends a message to the DLQ after
five receives. Generated reviews are artifacts only and are not posted to
GitHub because GitHub and AWS state cannot be committed atomically.

## Local setup

Use Python 3.12, matching the Lambda runtime:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
gh auth login
```

Set `person.github_username` and `repos` in the git-ignored `config.yaml`.
Historical collection uses the authenticated `gh` CLI. Azure Foundry accepts
either `AZURE_OPENAI_API_KEY` or `DefaultAzureCredential` (`az login`) locally.

Run the batch pipeline:

```bash
python main.py --config config.yaml
python main.py --config config.yaml --skip-collect
python main.py --config config.yaml --skip-analyze
```

Enable `aws_opensearch` in the config and use an AWS profile with OpenSearch
data access to index and retrieve review notes:

```bash
python main.py --config config.yaml --search "missing authorization check"
```

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
| `PR_TO_SKILL_REVIEWER` | Yes | Trusted reviewer's GitHub login |
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

The GitHub App needs Metadata read, Contents read, and Pull requests read.

## Runtime environment

SAM supplies AWS resource addresses and deployment settings to Lambda. These
are documented for troubleshooting and non-SAM hosts; do not set them as
GitHub Environment values unless replacing the template's wiring.

| Runtime variable | Owner / meaning |
| --- | --- |
| `ARTIFACT_BUCKET`, `ARTIFACT_PREFIX` | S3 artifact location; prefix defaults to `artifacts` |
| `DELIVERY_TABLE` | DynamoDB receipt and lock table |
| `EVENT_QUEUE_URL` | SQS URL used by webhook Lambda |
| `AWS_OPENSEARCH_ENABLED` | Must be `true` in the worker |
| `AWS_OPENSEARCH_ENDPOINT` | OpenSearch Serverless collection endpoint |
| `AWS_OPENSEARCH_INDEX` | Defaults to `pr-review-notes` |
| `AWS_OPENSEARCH_SERVICE` | `aoss` for Serverless |
| `AWS_REGION` | Lambda-provided AWS region |
| `PR_TO_SKILL_CONFIG` | Packaged YAML path, defaults to `config.example.yaml` |
| `PR_TO_SKILL_OPENAI_TIMEOUT` | Per-request cap, maximum `600` seconds |
| `PR_TO_SKILL_WORKFLOW_DEADLINE` | Workflow cap, maximum `780` seconds |
| `AZURE_OPENAI_API_MODE` | Optional; defaults from YAML to `responses` |
| `AGENT_MAX_OUTPUT_TOKENS` | Optional model output override |

## Output

- `output/raw_comments.json`: normalized historical feedback.
- `output/notes.json` and `output/notes.md`: AI-annotated review guidance.
- `output/SKILL.md`: consolidated review skill.
- `s3://<artifact-bucket>/artifacts/reviews/pr-<number>.md`: generated review.
- Canonical webhook history artifacts live under the same S3 prefix.

## Limitations

- The deployed demo supports exactly one repository and one trusted reviewer.
- Worker reserved concurrency is one. Cross-service S3, DynamoDB, OpenSearch,
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