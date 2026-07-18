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
```

Only *closed* PRs are scanned (open PRs are skipped by design, since a person
who's left the team won't have any open PRs with live comments).

Matching is by GitHub **login**, not email — the GitHub comment/review API
only exposes `user.login`, not email addresses. The email in the config is
kept for your own record-keeping.

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
