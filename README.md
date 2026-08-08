# pr-review-profiler

Mines a person's historical PR review comments (across any set of repos) and
turns them into a reusable `SKILL.md` encoding their engineering review
standards. Built for cases like: an engineer left the team, and you want to
preserve their review standards as a skill for Claude Code / Copilot / Codex
going forward.

## How it works

```
gh cli (closed PRs, reviews, comments)
        |
        v
[1] collect  -> output/raw_comments.json          (no AI, just GitHub data)
        |
        v
[2] analyze  -> output/notes.json, notes.md       (AI: infers original issue,
        |                                           requested change, why,
        |                                           category, severity per
        |                                           comment)
        v
[3] synthesize -> output/SKILL.md                 (AI: rolls up all notes into
                                                     one banded checklist skill)
  |
  +-------> Azure AI Search (optional vector index for retrieval)
```

Only *closed* PRs are scanned (open PRs are skipped by design, since a person
who's left the team won't have any open PRs with live comments).

Matching is by GitHub **login**, not email — the GitHub comment/review API
only exposes `user.login`, not email addresses. The email in the config is
kept for your own record-keeping.

## Event-driven Azure demo

The repository also contains a one-repository, Azure-native event path modeled
after the larger PR Review Intelligence System:

```text
GitHub webhook
  -> Azure Function HTTP trigger (raw-body HMAC verification)
  -> Azure Storage Queue (returns HTTP 202)
  -> Azure Function queue trigger
       pull_request opened/reopened/synchronize/ready_for_review
         -> retrieve repository-scoped Azure AI Search evidence
         -> generate an advisory review with Azure OpenAI
         -> save reviews/pr-<number>.md in Azure Blob Storage
       pull_request_review submitted/edited/dismissed
       pull_request_review_comment created/edited/deleted
         -> refetch that PR's complete review evidence
         -> replace/delete notes by stable GitHub numeric ID
         -> update Azure AI Search vectors
         -> regenerate notes.json and SKILL.md in Azure Blob Storage
```

Completed GitHub delivery IDs are stored as Blob receipts. A queue retry after
a successful run is ignored, while a failed run remains retryable. This is an
initial demo boundary: it is single-repository and uses one configured trusted
reviewer. The larger system's multi-tenant trust transitions, recurrence gate,
inline anchors, lifecycle administration, and deterministic contract checks are
deliberately out of scope here.

## Setup

```bash
python3 -m pip install -r requirements.txt

# GitHub auth (uses whatever you already have)
gh auth login
gh auth status   # confirm it can see the target repos
```

Create your local config from the template and fill in your GitHub username
and repos:

```bash
cp config.example.yaml config.yaml
# edit config.yaml: person.github_username and repos
```

`config.yaml` is git-ignored, so your real values are never committed.

### Azure OpenAI authentication

The pipeline supports two authentication methods and picks one automatically:

1. **API key (used if configured).** Set an Azure OpenAI API key — preferably
   via the environment so the secret never touches disk:

   ```bash
   cp .env.example .env         # then edit .env, or export the vars directly
   export AZURE_OPENAI_API_KEY="<your-key>"
   ```

   You may instead set `azure_openai.api_key` in `config.yaml`, but the
   environment variable takes precedence and is preferred. The key is never
   logged, printed, or written to any output file.

2. **Azure AD / Entra ID (fallback).** If no API key is configured, the
   existing `DefaultAzureCredential` chain is used — sign in with Azure CLI
   before running:

   ```bash
   az login
   ```

Azure connection settings default to the values in `config.yaml` and can be
overridden with `AZURE_OPENAI_ENDPOINT` (or `ENDPOINT_URL`),
`AZURE_OPENAI_DEPLOYMENT` (or `DEPLOYMENT_NAME`), `AZURE_OPENAI_API_VERSION`,
`AZURE_OPENAI_API_MODE`, `AGENT_REQUEST_TIMEOUT`, and `AGENT_MAX_OUTPUT_TOKENS`.
See [.env.example](.env.example) for the full list of supported variables.

## Run

```bash
python3 main.py --config config.yaml
```

To index analyzed notes for retrieval, configure `azure_search` and an Azure
OpenAI embedding deployment, then set `enabled: true`. The pipeline creates or
updates the index and upserts each note. Retrieve relevant evidence while
reviewing a PR with:

```bash
python3 main.py --config config.yaml --search "missing authorization check"
```

Before enabling webhooks for an existing history set, bootstrap its durable
artifacts into the same Storage account used by the Function App:

```bash
python3 main.py --config config.yaml --skip-collect --skip-analyze \
  --sync-azure-artifacts
```

This prevents the first feedback event from rebuilding `SKILL.md` from only
the newly changed PR.

`AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_INDEX`, `AZURE_SEARCH_API_KEY`,
`AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, and
`AZURE_OPENAI_EMBEDDING_DIMENSIONS` can override the YAML settings. Without a
search API key, Azure AI Search uses `DefaultAzureCredential`.

### Run the Azure Functions demo locally

Use Python 3.12 for Azure Functions deployment compatibility. Install Azure
Functions Core Tools and Azurite, copy `local.settings.example.json` to the
git-ignored `local.settings.json`, then run Azurite and the Functions host:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
azurite
func start
```

Configure the GitHub App webhook URL as
`https://<function-app>.azurewebsites.net/api/github/webhook`. Subscribe to:

- Pull requests
- Pull request reviews
- Pull request review comments

Required GitHub App repository permissions are Metadata read, Contents read,
and Pull requests read. Generated reviews remain in Blob Storage in this demo;
they are not posted automatically because GitHub comments and Azure state
cannot be committed atomically. Store the webhook secret and App private key as
Azure Key Vault references in Function App settings rather than committing them.

For Azure deployment, set `PR_TO_SKILL_CONFIG=config.example.yaml` and override
the placeholders with Function App settings: `PR_TO_SKILL_REPO`,
`PR_TO_SKILL_REVIEWER`, Azure OpenAI/Search settings, GitHub App settings, and
`AzureWebJobsStorage`. Assign the Function managed identity:

- `Cognitive Services OpenAI User` on the Azure OpenAI resource
- `Search Index Data Contributor` and `Search Service Contributor` on Azure AI Search
- Storage queue/blob data access when identity-based storage settings are used

Set `AZURE_STORAGE_ACCOUNT_URL=https://<account>.blob.core.windows.net` when
the artifact and delivery-receipt adapters should use managed identity. The
Functions queue binding can use the corresponding `AzureWebJobsStorage__*`
identity-based settings supported by Azure Functions.

The HTTP Function performs no model or GitHub work. Queue retries and poison
messages follow `host.json`; failed messages move to the runtime's
`pr-review-events-poison` queue after five attempts.

Deploy the queue worker on an Azure Functions Premium plan. Its configured
60-minute timeout is not supported by the Consumption plan. Individual Azure
OpenAI requests are capped at 600 seconds through
`PR_TO_SKILL_OPENAI_TIMEOUT`, leaving time for bounded analysis, synthesis,
storage commits, and retries within the host deadline. The complete workflow is
capped at 2,700 seconds through `PR_TO_SKILL_WORKFLOW_DEADLINE`, and one PR is
limited to 100 review items by default. Configure the Function App setting
`WEBSITE_MAX_DYNAMIC_APPLICATION_SCALE_OUT=1` for this initial single-worker
demo; production scale-out requires a transactional workflow store rather than
cross-service Blob lease coordination.

Resumable flags, since scanning big repos + hundreds of LLM calls is slow:

```bash
python3 main.py --config config.yaml --skip-collect   # reuse raw_comments.json
python3 main.py --config config.yaml --skip-analyze   # reuse notes.json, just re-synthesize
```

## Output

- `output/raw_comments.json` — every raw comment/review/issue-comment by the
  person, with repo, PR, file, diff hunk, and body.
- `output/notes.json` / `output/notes.md` — the same comments, but each one
  annotated by AI with category, original issue, requested change, rationale,
  and severity (blocking / suggestion / nitpick).
- `output/SKILL.md` — the final rolled-up skill, grouped into bands by
  category, ready to drop into a `.claude/skills/` (or Copilot/Codex
  equivalent) directory.

## Notes / limitations

- Large orgs with hundreds of closed PRs per repo will make a lot of GitHub
  API calls. Each JSON page emitted by `gh api --paginate` is decoded and
  flattened; rate limits still apply, so consider setting
  `github.updated_after` to bound the scan.
- Analysis responses are schema-validated and retried up to
  `analysis.max_attempts`. The run stops if a batch remains invalid so a
  partial `SKILL.md` is never silently produced.
- Collection uses `github.workers` threads (default 8), while analysis and
  map-stage synthesis use 4 Azure worker threads by default. These operations
  are network-bound, so threads overlap API waits despite Python's GIL. Keep
  the Azure count within the deployment's requests-per-minute and
  tokens-per-minute quotas.
- The synthesizer auto-switches to a map-reduce pass (chunk + merge) once
  note count exceeds `max_notes_per_call` (400) in `skill_synthesizer.py`, so
  it stays reliable even for very prolific reviewers.
- Nothing here is repo- or person-specific — swap `github_username` and
  `repos` in the config to run this against anyone else.
