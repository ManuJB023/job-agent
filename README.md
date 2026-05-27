# Job-Agent: Autonomous Cloud-Native Job Matching Pipeline

> An event-driven AWS application that ingests job postings from multiple sources twice daily, scores each posting against four resume variants using Claude, and delivers a ranked, reasoned digest by email.

[![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC?logo=terraform)](https://terraform.io)
[![AWS](https://img.shields.io/badge/AWS-Lambda%20%7C%20DynamoDB%20%7C%20SES%20%7C%20EventBridge-FF9900?logo=amazonaws)](https://aws.amazon.com)
[![Claude](https://img.shields.io/badge/AI-Claude%20Haiku%204.5-D97757)](https://anthropic.com)
[![CI](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?logo=githubactions)](https://github.com/features/actions)

---

## What it does

Most job seekers spend hours every day refreshing the same five tabs. This pipeline replaces that with a serverless agent that runs on a schedule, collects relevant postings, evaluates fit against a portfolio of resume variants, and reports back.

A typical morning digest:

```
Job-Agent Digest — 10 matches (May 27, 01PM UTC)
─────────────────────────────────────────────────
[92] Cloud Platform Engineer · SLB · Houston, TX
     Resume: cloud_devops.md
     Why: 15+ yrs O&G + AWS/Terraform — direct fit for SLB
     digital transformation. Strong match on Lambda, ECS,
     IaC. Gap: Kubernetes (mentioned as plus).

[87] DevOps Engineer · Halliburton Digital · Remote
     Resume: cloud_devops.md
     Why: Industrial systems background + GitHub Actions +
     containerization. Salary range matches target.

[81] SRE — Edge & Field Systems · Chevron Technology Ventures
     Resume: sre.md
     Why: SCADA / field operations crossover. Reliability
     mindset from 24/7 wireline ops directly relevant.
```

Each digest entry includes Claude's full reasoning, matched skills, and honest gap analysis so you can decide whether to invest the application time.

## Architecture

```
                    ┌─────────────────────┐
                    │   EventBridge       │
                    │  cron(0 12,0 * * ?) │   12:00 & 00:00 UTC
                    └──────────┬──────────┘   (7am/7pm America/New_York)
                               │
                               ▼
                    ┌─────────────────────┐       ┌──────────────────┐
                    │  collector Lambda   │──────▶│  JobSpy          │
                    │  Python 3.12        │       │  (LinkedIn,      │
                    │  1024MB, 600s       │       │   Indeed, Glass, │
                    │  + pandas layer     │       │   ZipRecruiter)  │
                    └──────────┬──────────┘       └──────────────────┘
                               │                  ┌──────────────────┐
                               ├─────────────────▶│  JSearch API     │
                               │                  │  (RapidAPI)      │
                               │                  └──────────────────┘
                               │                  ┌──────────────────┐
                               ├─────────────────▶│  Greenhouse /    │
                               │                  │  Lever / Ashby   │
                               │                  │  direct ATS APIs │
                               │                  └──────────────────┘
                               ▼
                    ┌─────────────────────┐
                    │   DynamoDB          │
                    │   jobs table        │◀──── deduplication
                    │   (PK: JOB#<hash>)  │      via content hash
                    │   60-day TTL        │
                    └──────────┬──────────┘
                               │ stream: NEW_IMAGE
                               ▼
                    ┌─────────────────────┐       ┌──────────────────┐
                    │   scorer Lambda     │──────▶│  Claude          │
                    │   512MB, 120s       │       │  Haiku 4.5       │
                    └──────────┬──────────┘       │  /v1/messages    │
                               │                  └──────────────────┘
                               ▼
                    ┌─────────────────────┐
                    │   DynamoDB          │  status: SCORED
                    │   (item updated)    │  fit_score, reasoning,
                    │                     │  best_resume_variant,
                    │                     │  matched_skills, gaps
                    └─────────────────────┘
                               │
                               │  EventBridge: cron(0 13,1 * * ?)
                               ▼
                    ┌─────────────────────┐       ┌──────────────────┐
                    │   notifier Lambda   │──────▶│  Amazon SES      │
                    │   512MB, 60s        │       │  HTML digest     │
                    └─────────────────────┘       └──────────────────┘
```

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for component-by-component detail.

## Tech stack

| Layer            | Tech                                              |
|------------------|---------------------------------------------------|
| Compute          | AWS Lambda (Python 3.12) + Lambda Layer (pandas/numpy) |
| Storage          | Amazon DynamoDB (on-demand, single-table design)  |
| Code artifacts   | Amazon S3 (Lambda packages + layer, served via `s3_bucket`/`s3_key`) |
| Scheduling       | Amazon EventBridge Scheduler                      |
| Notification     | Amazon SES                                        |
| Secrets          | AWS Systems Manager Parameter Store (SecureString)|
| Observability    | CloudWatch Logs + Metrics + Alarms                |
| IaC              | Terraform 1.6+ (AWS provider 5.x)                 |
| CI/CD            | GitHub Actions                                    |
| AI matching      | Claude Haiku 4.5 via Anthropic Python SDK         |
| Job sources      | JobSpy 1.1.x, JSearch API, Greenhouse/Lever/Ashby |

## Quick start

```bash
# 1. Clone and install local dev deps
git clone https://github.com/<you>/job-agent.git
cd job-agent
make install

# 2. Set required secrets (creates SSM SecureString parameters)
make secrets ANTHROPIC_API_KEY=sk-ant-... JSEARCH_API_KEY=...

# 3. Configure
cp terraform/prod.example.tfvars terraform/prod.tfvars
$EDITOR terraform/prod.tfvars      # SES emails, threshold, region
$EDITOR config/config.yaml         # target companies, search terms
$EDITOR resumes/*.md               # your four resume variants

# 4. Deploy (provisions AWS infra + uploads Lambda packages)
make deploy

# 5. Verify SES — click the link sent to NOTIFICATION_EMAIL
# 6. Run once manually to verify
make invoke-collector
```

**Pre-deployment checklist** (these caught us during build — save yourself the time):

- **Gmail strongly preferred** over ProtonMail / pm.me for `NOTIFICATION_EMAIL`. ProtonMail silently drops SES emails from new senders even after verification (zero bounces, zero rejects, just disappear). Gmail accepts reliably.
- **Both `SENDER_EMAIL` and `NOTIFICATION_EMAIL` must be SES-verified** in sandbox mode. They can be the same address; production accounts can skip recipient verification.
- **Anthropic Tier 1 rate-limits Haiku at 50 req/min.** If you bulk-rescore (e.g. after a key rotation), throttle to ~40 req/min or you'll hit 429s.
- **Lambda's 262MB unzipped limit is tight.** The Makefile build target strips `pandas`, `numpy`, `tls_client`, and unused `botocore` service models from the collector package because those packages are served via Lambda Layer or never used.
- **Windows users**: prefix AWS CLI commands with `MSYS_NO_PATHCONV=1` in Git Bash, otherwise SSM parameter names like `/job-agent/anthropic_api_key` get rewritten to Windows-style paths.

## Cost estimate (per month, at default config)

Real numbers from this deployment, not theoretical:

| Item                                 | Volume                                    | Cost       |
|--------------------------------------|-------------------------------------------|------------|
| Lambda invocations                   | ~120 collector + ~6,000 scorer + ~60 notifier | $0.20  |
| DynamoDB on-demand                   | ~6,000 writes, ~12,000 reads              | $0.30      |
| EventBridge                          | scheduled rules                           | $0.00      |
| SES                                  | ~60 emails                                | $0.01      |
| CloudWatch Logs                      | ~500 MB ingestion                         | $0.25      |
| S3                                   | ~200 MB Lambda artifacts                  | $0.01      |
| Claude Haiku 4.5 (input + output)    | ~6,000 jobs scored                        | $10 – $20  |
| JSearch API (Pro tier)               | optional fallback                         | $0 – $25   |
| **Estimated steady-state total**     |                                           | **$11 – $46** |

JobSpy and Greenhouse/Lever/Ashby endpoints are free. The dominant variable cost is Claude API spend, driven by job volume and prompt length. For the first few days expect higher Claude spend if you rescore historical jobs.

> **Note on first deployment**: budget ~$20 for Anthropic credits to comfortably cover initial deployment, debugging, and any historical rescoring. Steady-state operation thereafter is ~$0.50/day at typical job-board volume.

## Repository layout

```
job-agent/
├── README.md                    # this file
├── ARCHITECTURE.md              # design decisions, trade-offs, future work
├── Makefile                     # install, test, build, deploy, invoke, logs
├── rescore.py                   # one-shot: reset SCORE_FAILED → re-trigger scorer
├── .github/workflows/
│   └── deploy.yml               # CI/CD: lint, test, terraform plan/apply
├── terraform/
│   ├── main.tf                  # provider + locals
│   ├── variables.tf
│   ├── outputs.tf
│   ├── dynamodb.tf              # jobs table + stream
│   ├── lambda.tf                # 3 functions + pandas layer
│   ├── s3.tf                    # artifacts bucket for Lambda packages
│   ├── eventbridge.tf           # scheduled rules + permissions
│   ├── iam.tf                   # least-privilege roles
│   └── ses.tf                   # verified identities
├── src/
│   ├── collector/handler.py     # ingests from JobSpy, JSearch, Greenhouse
│   ├── scorer/handler.py        # invokes Claude for each new job
│   ├── notifier/handler.py      # builds + sends HTML digest email
│   └── shared/                  # models, DDB helpers, structured logging
├── resumes/                     # markdown resume variants (gitignored — see resumes/*.example.md)
│   ├── cloud_devops.md
│   ├── sre.md
│   ├── fullstack.md
│   └── industrial_iot.md
├── config/
│   └── config.yaml              # search terms, target companies, defaults
├── scripts/
│   └── seed_target_companies.py # bootstrap Greenhouse company list
└── tests/                       # pytest unit + integration
```

## Why this exists (and why it's a portfolio piece)

This project was built to solve a real problem — automating job discovery across five fragmented job boards — but it doubles as a demonstration of the engineering practices it markets:

- **Infrastructure as Code**: every AWS resource provisioned by Terraform; reproducible across regions and accounts. `.terraform.lock.hcl` committed for deterministic provider versions.
- **Least-privilege IAM**: each Lambda has a scoped role; SES `SendEmail` restricted by identity ARN; DynamoDB actions scoped to the specific table; no `*` policies.
- **Event-driven design**: DynamoDB streams decouple ingestion from scoring; the collector and scorer fail independently and retry independently.
- **Cost-conscious**: on-demand DynamoDB, Haiku-tier LLM, no always-on compute, S3 + Lambda Layers used to stay under Lambda's 262MB unzipped limit.
- **Operationally observable**: structured JSON logging, CloudWatch alarms on Lambda error rates and throttles, `make logs-*` shortcuts.
- **CI/CD**: GitHub Actions runs lint, type-check, unit tests, then `terraform plan` on PR and `apply` on merge to `main`.
- **Documented**: this README, an architecture doc, inline docstrings, and operational runbooks.

## Lessons from production deployment

The system works as designed. The path to "works as designed" had real obstacles worth documenting:

1. **Lambda packages outgrew the 262MB unzipped limit.** Initial collector package was 282MB (mostly `pandas`, `numpy`, `tls_client` from JobSpy). Fixed by extracting `pandas`/`numpy` into a Lambda Layer and stripping `tls_client` (89MB, unused for our routes) plus several unused `botocore` service models from the collector zip. Final collector is 53MB + ~120MB layer = ~173MB combined, comfortably under the limit.

2. **Anthropic Tier 1 rate limit is 50 req/min for Haiku.** Bulk operations (e.g., rescoring 1,500+ stale jobs after a key rotation) will hit 429s if you fan out parallel Lambda invokes. The included `rescore.py` throttles to 40 req/min for hands-off recovery.

3. **ProtonMail silently drops SES emails.** Verification succeeds, SES reports `DeliveryAttempts: N, Bounces: 0, Rejects: 0`, and the emails never arrive in `pm.me`. Switching `NOTIFICATION_EMAIL` to Gmail resolved this immediately. Documented in the pre-deployment checklist.

4. **Lambda caches SSM parameter reads** for the lifetime of the container. Rotating the Anthropic API key requires forcing a Lambda config update (e.g., `update-function-configuration --description "key-refresh-$(date +%s)"`) to trigger a fresh container.

5. **Score quality depends on resume markdown quality.** Claude can only score against what it sees. Vague or thin resume variants produce mediocre matches. Be specific: tools, scopes, real numbers, named projects.

## Future work

- React dashboard (S3 + CloudFront + API Gateway) for browsing all scored jobs and overriding the threshold per session.
- Embedding-based pre-filter (Voyage AI or OpenAI text-embedding-3) before LLM scoring to reduce token cost at higher job volumes.
- Auto-application generator: tailored cover letter + tailored resume per match, drafted as Gmail drafts via the Gmail API.
- Slack delivery option alongside SES.
- Per-company application-history tracking, with re-application cooldown.
- Embedding-based "similar jobs you applied to and got responses from" recommender.

## License

MIT