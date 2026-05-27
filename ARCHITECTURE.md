# Architecture

## Design principles

1. **Event-driven, not polling within Lambda.** EventBridge fires the collector. DynamoDB streams fire the scorer. No Lambda sits waiting on another Lambda.
2. **Idempotent collection.** Re-running the collector with the same source data produces no duplicate writes. Achieved via content-hash primary keys.
3. **Decoupled stages.** A failing scorer doesn't block ingestion; a failing notifier doesn't block scoring. Each stage is independently retriable.
4. **Cheap by default.** On-demand DynamoDB, Haiku-tier LLM, no NAT gateways, no always-on compute. Scales to zero between runs.

## Data flow

### Stage 1 — Collection

`EventBridge → collector Lambda → DynamoDB`

The collector runs on a cron schedule (12:00 UTC and 00:00 UTC by default — roughly 7am/7pm in `America/New_York`). It executes three source plugins in parallel:

| Source            | Library / endpoint                                | Why included                                     |
|-------------------|---------------------------------------------------|--------------------------------------------------|
| **JobSpy**        | `python-jobspy` → LinkedIn / Indeed / Glassdoor / ZipRecruiter / Google Jobs | Broad coverage, free, MIT-licensed.              |
| **JSearch**       | RapidAPI HTTP, backed by Google for Jobs          | Resilient fallback when JobSpy hits rate limits. |
| **Greenhouse / Lever / Ashby** | Public ATS JSON endpoints              | Highest signal-to-noise; targeted at companies most relevant to the candidate's background. |

Each posting is normalized into a `JobPosting` dataclass and hashed deterministically (SHA-1 of `company + title + location + first 200 chars of description`). The hash is the DynamoDB primary key, which gives us free deduplication across sources and across runs — if Indeed and LinkedIn both surface the same SLB role, it's stored once.

New items are written with `status = "PENDING"` and `collected_at = <now>`. Existing items are skipped via a conditional write (`attribute_not_exists(pk)`).

### Stage 2 — Scoring

`DynamoDB stream (NEW_IMAGE) → scorer Lambda → DynamoDB (update)`

When a new job is inserted, the DynamoDB stream triggers the scorer. The scorer:

1. Loads the four resume markdown files from the Lambda deployment package.
2. Calls Claude Haiku 4.5 once per job with a structured prompt: "Here are four resume variants. Here is a job posting. Choose the best variant, score the fit 0–100, list matched skills, list gaps, and explain in two sentences why this role fits the candidate."
3. Parses the JSON response and updates the job item: `status = "SCORED"`, plus `fit_score`, `best_resume_variant`, `matched_skills`, `gaps`, `reasoning`.

The prompt enforces JSON output via Claude's structured-output guidance. If parsing fails, the item is marked `status = "SCORE_FAILED"` and surfaced to CloudWatch for inspection — never silently dropped.

### Stage 3 — Notification

`EventBridge → notifier Lambda → SES`

The notifier runs one hour after each collector run. It queries DynamoDB via a GSI on `status + fit_score` for items where `status = "SCORED"` and `fit_score >= threshold` (default 70), sorted descending. It builds an HTML email digest and sends via SES. Successfully notified items are updated to `status = "SENT"` to avoid re-sending.

## DynamoDB schema

Single-table design.

| Attribute              | Type   | Purpose                                              |
|------------------------|--------|------------------------------------------------------|
| `pk`                   | S      | `JOB#<sha1>` — content hash                          |
| `sk`                   | S      | `META` (room for `APPLICATION#...` items later)      |
| `status`               | S      | `PENDING` / `SCORED` / `SCORE_FAILED` / `SENT`       |
| `fit_score`            | N      | 0–100, set by scorer                                 |
| `collected_at`         | S      | ISO 8601                                             |
| `source`               | S      | `jobspy:linkedin` / `jsearch` / `greenhouse:slb` etc |
| `company`              | S      |                                                      |
| `title`                | S      |                                                      |
| `location`             | S      |                                                      |
| `remote`               | BOOL   |                                                      |
| `apply_url`            | S      |                                                      |
| `description`          | S      | full text (truncated to 30KB)                        |
| `posted_at`            | S      | ISO 8601, when source reports it                     |
| `best_resume_variant`  | S      | `cloud_devops` / `sre` / `fullstack` / `industrial_iot` |
| `matched_skills`       | SS     | string set                                           |
| `gaps`                 | SS     | string set                                           |
| `reasoning`            | S      | 2-sentence explanation                               |

### Global Secondary Index

`status-fit_score-index`:
- Partition key: `status`
- Sort key: `fit_score` (Number, descending queries)

The notifier queries this GSI for `status = SCORED, fit_score >= 70`.

### TTL

`expires_at` attribute set to `collected_at + 60 days`. Old postings auto-delete.

## IAM

Each Lambda has its own role with the minimum required permissions:

- **collector**: `dynamodb:PutItem` on jobs table; `ssm:GetParameter` on `/job-agent/*`; `logs:*` on its own log group.
- **scorer**: `dynamodb:UpdateItem` on jobs table; `ssm:GetParameter` on `/job-agent/anthropic_api_key`; permission to read the DynamoDB stream; logs.
- **notifier**: `dynamodb:Query` on jobs table + GSI; `dynamodb:UpdateItem`; `ses:SendEmail` scoped to the verified identity; logs.

No `Resource: "*"` policies anywhere except CloudWatch Logs (which Lambda execution requires).

## Failure modes & runbooks

### Collector returns zero jobs

Most common cause: JobSpy hit LinkedIn's rate limit (~10 pages per IP). The collector falls through to JSearch + Greenhouse automatically. If all three return zero, an alarm fires.

Diagnose: CloudWatch Logs for `collector` Lambda will show per-source counts. If JobSpy throws `403 Forbidden`, this is the rate limit. Solutions: (a) wait — limits reset hourly per IP, (b) add residential proxies via the `proxies` parameter, (c) reduce `results_wanted` per search term.

### Scorer fails to parse Claude response

The scorer asks Claude for JSON. If parsing fails, the item is marked `SCORE_FAILED` rather than retried indefinitely. Inspect via DynamoDB scan on `status = SCORE_FAILED`. Re-run the scorer manually with `make invoke-scorer-retry`. Persistent failures usually mean the prompt needs tightening — see `src/scorer/prompts.py`.

### SES email not received

SES starts in sandbox mode. Until you request production access, only verified recipients receive mail. Sender and recipient must both be verified. Check the SES console.

## Trade-offs taken

- **No embedding pre-filter.** At ~200 jobs/day the LLM cost is ~$5/month with Haiku; adding embedding infrastructure isn't worth the savings yet. Above ~1,000 jobs/day, revisit.
- **Single AWS region.** No multi-region replication. This is a personal tool; if `us-east-1` is down, checking jobs can wait.
- **No VPC.** Lambdas run in the AWS-managed network. No data here is sensitive enough to warrant the cold-start penalty of VPC Lambdas.
- **Markdown resumes, not parsed PDFs.** Keeping resumes as markdown means edits are git-trackable and the LLM gets clean input. The trade-off is you maintain markdown copies alongside the PDFs.

## Future evolution

The single-table schema reserves `sk = APPLICATION#<timestamp>` for tracking applications submitted per job. The collector and scorer don't write these; a future "applier" stage would. This is why the schema uses `pk + sk` even though the current code only ever writes `sk = META`.
