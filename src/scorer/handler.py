"""Scorer Lambda.

Triggered by DynamoDB stream INSERT events from the jobs table. For each
new job:
  1. Loads the four resume markdown variants (bundled in deploy package).
  2. Calls Claude Haiku 4.5 with a structured prompt.
  3. Parses the JSON response and updates the job item with fit_score,
     best_resume_variant, matched_skills, gaps, and reasoning.

Designed to be cheap: ~3-4K input tokens per job, ~300 output tokens.
At Haiku pricing that's well under $0.01 per job.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from shared import (
    ScoreResult,
    configure_logging,
    get_ssm_parameter,
    get_table,
    log_json,
    mark_score_failed,
    mark_scored,
)

log = configure_logging()

RESUME_DIR = Path(__file__).parent / "resumes"
RESUME_VARIANTS = ("cloud_devops", "sre", "fullstack", "industrial_iot")


# ─────────────────────────────────────────────────────────────────────────────
# Resume loading (cached for the life of the warm container)
# ─────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def load_resumes() -> dict[str, str]:
    resumes: dict[str, str] = {}
    for variant in RESUME_VARIANTS:
        path = RESUME_DIR / f"{variant}.md"
        if path.exists():
            resumes[variant] = path.read_text(encoding="utf-8")
        else:
            log_json(log, "warning", "resume_missing", variant=variant)
    if not resumes:
        raise RuntimeError("no resume files found in deploy package")
    return resumes


# ─────────────────────────────────────────────────────────────────────────────
# Claude client
# ─────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def get_anthropic_client():
    import anthropic
    key_param = os.environ["ANTHROPIC_API_KEY_PARAM"]
    api_key = get_ssm_parameter(key_param)
    return anthropic.Anthropic(api_key=api_key)


SYSTEM_PROMPT = """You are a career-fit scoring engine for one specific candidate.

You will be given:
  1. Four resume variants for the candidate:
       - cloud_devops    (general AWS/DevOps/Terraform/CI-CD roles)
       - sre             (reliability, infrastructure ops, incident response)
       - fullstack       (Node.js + React + AWS app development)
       - industrial_iot  (cloud + OT/SCADA/edge telemetry, oil & gas digital)
  2. A single job posting.

Your job: judge how well this candidate fits this role, pick which of the four resumes best matches, and explain in two sentences.

Be strict. A 90+ score means the candidate is clearly qualified and would likely get a callback. A 70-89 score means a credible application worth submitting. Below 70 means likely a waste of time. Heavily penalize roles that demand 5+ years of professional IT experience the candidate doesn't have. Heavily reward roles that value industrial / oil & gas / field operations background as a plus, or that accept bootcamp + certifications + portfolio in lieu of years of experience.

Variant selection guidance:
  - Pick `industrial_iot` whenever the role mentions OT, SCADA, MQTT, OPC UA, Modbus, edge computing, sensor telemetry, digital twin, oil & gas, energy, manufacturing, or industrial automation — even if the bulk of the role is cloud. This is the candidate's strongest differentiator.
  - Pick `sre` when the role emphasizes reliability, on-call, incident response, observability, or production operations more than greenfield build.
  - Pick `fullstack` only when the role is primarily application development (Node/React) with cloud as supporting.
  - Default to `cloud_devops` for generalist cloud/DevOps roles without an industrial or app-development slant.

Return ONLY a valid JSON object, no prose before or after, in exactly this shape:

{
  "fit_score": 0-100,
  "best_resume_variant": "cloud_devops" | "sre" | "fullstack" | "industrial_iot",
  "matched_skills": ["...", "..."],
  "gaps": ["...", "..."],
  "reasoning": "<two sentences, max 280 chars>"
}"""


def build_user_message(resumes: dict[str, str], job: dict) -> str:
    resumes_block = "\n\n".join(
        f"=== Resume variant: {variant} ===\n{text}"
        for variant, text in resumes.items()
    )

    job_block = f"""=== Job posting ===
Company: {job['company']}
Title: {job['title']}
Location: {job['location']}
Remote: {job.get('remote', False)}
Source: {job['source']}
Description:
{job['description'][:8000]}"""

    return f"{resumes_block}\n\n{job_block}"


# ─────────────────────────────────────────────────────────────────────────────
# Stream event parsing
# ─────────────────────────────────────────────────────────────────────────────
def extract_job_from_stream_record(record: dict) -> dict | None:
    """DynamoDB stream NEW_IMAGE is in AWS attribute-value format."""
    new_image = record.get("dynamodb", {}).get("NewImage")
    if not new_image:
        return None
    return {
        "pk": _ddb_val(new_image.get("pk")),
        "company": _ddb_val(new_image.get("company")),
        "title": _ddb_val(new_image.get("title")),
        "location": _ddb_val(new_image.get("location")),
        "description": _ddb_val(new_image.get("description")),
        "source": _ddb_val(new_image.get("source")),
        "remote": _ddb_val(new_image.get("remote"), default=False),
        "apply_url": _ddb_val(new_image.get("apply_url")),
    }


def _ddb_val(attr: dict | None, default=""):
    if not attr:
        return default
    if "S" in attr:
        return attr["S"]
    if "N" in attr:
        return float(attr["N"])
    if "BOOL" in attr:
        return attr["BOOL"]
    return default


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────
JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def score_job(job: dict) -> ScoreResult:
    resumes = load_resumes()
    client = get_anthropic_client()
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    response = client.messages.create(
        model=model,
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_message(resumes, job)}],
    )

    raw = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()

    # Tolerate a stray ```json fence.
    match = JSON_BLOCK_RE.search(raw)
    if not match:
        raise ValueError(f"no JSON object in Claude response: {raw[:300]}")
    payload = json.loads(match.group(0))

    score = ScoreResult(
        fit_score=int(payload["fit_score"]),
        best_resume_variant=payload["best_resume_variant"],
        matched_skills=[str(s) for s in payload.get("matched_skills", [])][:15],
        gaps=[str(s) for s in payload.get("gaps", [])][:10],
        reasoning=str(payload.get("reasoning", ""))[:500],
    )
    score.validate()
    return score


# ─────────────────────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────────────────────
def handler(event, context):
    records = event.get("Records", [])
    log_json(log, "info", "scorer_start", batch_size=len(records))
    table = get_table()

    succeeded = 0
    failed = 0

    for record in records:
        if record.get("eventName") != "INSERT":
            continue

        job = extract_job_from_stream_record(record)
        if not job or not job.get("pk"):
            continue

        try:
            score = score_job(job)
            mark_scored(table, job["pk"], score)
            succeeded += 1
            log_json(log, "info", "job_scored",
                     pk=job["pk"], score=score.fit_score,
                     variant=score.best_resume_variant)
        except Exception as e:
            failed += 1
            log_json(log, "error", "job_score_failed",
                     pk=job["pk"], error=str(e))
            try:
                mark_score_failed(table, job["pk"], str(e))
            except Exception as ee:
                log_json(log, "error", "mark_failed_errored", error=str(ee))

    log_json(log, "info", "scorer_done", succeeded=succeeded, failed=failed)
    return {"succeeded": succeeded, "failed": failed}
