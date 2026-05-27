"""Shared data models and DynamoDB helpers for all three Lambdas.

Kept deliberately dependency-light: only boto3 and stdlib. Each Lambda
deploys this module via the build step (see Makefile).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Statuses
# ─────────────────────────────────────────────────────────────────────────────
STATUS_PENDING = "PENDING"
STATUS_SCORED = "SCORED"
STATUS_SCORE_FAILED = "SCORE_FAILED"
STATUS_SENT = "SENT"


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class JobPosting:
    """A normalized job posting, ready to write to DynamoDB."""

    company: str
    title: str
    location: str
    description: str
    apply_url: str
    source: str                         # e.g. "jobspy:linkedin"
    remote: bool = False
    posted_at: str | None = None        # ISO 8601 from source
    salary_min: int | None = None
    salary_max: int | None = None

    def content_hash(self) -> str:
        """Deterministic dedup key. Same posting from two sources → same hash."""
        key = "|".join([
            self.company.strip().lower(),
            self.title.strip().lower(),
            self.location.strip().lower(),
            (self.description or "")[:200].strip().lower(),
        ])
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    def to_dynamo_item(self, ttl_days: int = 60) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        expires = int((now + timedelta(days=ttl_days)).timestamp())
        # Truncate description to 30KB to stay well under DynamoDB's 400KB
        # item limit even with full LLM output added later.
        desc = (self.description or "")[:30_000]
        item = {
            "pk": f"JOB#{self.content_hash()}",
            "sk": "META",
            "status": STATUS_PENDING,
            "collected_at": now.isoformat(),
            "expires_at": expires,
            "source": self.source,
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "description": desc,
            "apply_url": self.apply_url,
            "remote": self.remote,
        }
        if self.posted_at:
            item["posted_at"] = self.posted_at
        if self.salary_min is not None:
            item["salary_min"] = self.salary_min
        if self.salary_max is not None:
            item["salary_max"] = self.salary_max
        return item


@dataclass
class ScoreResult:
    """LLM scoring output."""

    fit_score: int                      # 0-100
    best_resume_variant: str            # cloud_devops | sre | fullstack | industrial_iot
    matched_skills: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    reasoning: str = ""

    def validate(self) -> None:
        if not 0 <= self.fit_score <= 100:
            raise ValueError(f"fit_score out of range: {self.fit_score}")
        if self.best_resume_variant not in {"cloud_devops", "sre", "fullstack", "industrial_iot"}:
            raise ValueError(f"unknown resume variant: {self.best_resume_variant}")


# ─────────────────────────────────────────────────────────────────────────────
# DynamoDB helpers
# ─────────────────────────────────────────────────────────────────────────────
def get_table():
    """Return the boto3 Table resource for the jobs table."""
    table_name = os.environ["JOBS_TABLE"]
    return boto3.resource("dynamodb").Table(table_name)


def put_job_if_new(table, job: JobPosting) -> bool:
    """Conditional put — returns True if inserted, False if it already existed."""
    item = job.to_dynamo_item()
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def mark_scored(table, pk: str, score: ScoreResult) -> None:
    table.update_item(
        Key={"pk": pk, "sk": "META"},
        UpdateExpression=(
            "SET #s = :scored, "
            "    fit_score = :fs, "
            "    best_resume_variant = :brv, "
            "    matched_skills = :ms, "
            "    gaps = :g, "
            "    reasoning = :r, "
            "    scored_at = :sa"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":scored": STATUS_SCORED,
            ":fs": score.fit_score,
            ":brv": score.best_resume_variant,
            # DynamoDB string sets can't be empty; fall back to a placeholder.
            ":ms": set(score.matched_skills) if score.matched_skills else {"_none_"},
            ":g": set(score.gaps) if score.gaps else {"_none_"},
            ":r": score.reasoning,
            ":sa": datetime.now(timezone.utc).isoformat(),
        },
    )


def mark_score_failed(table, pk: str, error: str) -> None:
    table.update_item(
        Key={"pk": pk, "sk": "META"},
        UpdateExpression="SET #s = :failed, score_error = :e, scored_at = :sa",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":failed": STATUS_SCORE_FAILED,
            ":e": error[:1000],
            ":sa": datetime.now(timezone.utc).isoformat(),
        },
    )


def mark_sent(table, pk: str) -> None:
    table.update_item(
        Key={"pk": pk, "sk": "META"},
        UpdateExpression="SET #s = :sent, sent_at = :sa",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":sent": STATUS_SENT,
            ":sa": datetime.now(timezone.utc).isoformat(),
        },
    )


def query_scored_above_threshold(table, threshold: int, limit: int = 25):
    """Query the GSI for SCORED jobs with fit_score >= threshold, top N."""
    response = table.query(
        IndexName="status-fit_score-index",
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("status").eq(STATUS_SCORED)
            & boto3.dynamodb.conditions.Key("fit_score").gte(Decimal(threshold))
        ),
        ScanIndexForward=False,  # descending by fit_score
        Limit=limit,
    )
    return response.get("Items", [])


# ─────────────────────────────────────────────────────────────────────────────
# Misc utilities
# ─────────────────────────────────────────────────────────────────────────────
def configure_logging() -> logging.Logger:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format='{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","message":%(message)s}',
    )
    return logging.getLogger("job-agent")


def log_json(log: logging.Logger, level: str, event: str, **fields) -> None:
    """Emit a structured log line as valid JSON.

    The base format string above wraps `message` in JSON, so we hand it a
    pre-encoded JSON object here.
    """
    payload = {"event": event, **fields}
    getattr(log, level.lower())(json.dumps(payload, default=str))


def get_ssm_parameter(name: str, decrypt: bool = True) -> str:
    ssm = boto3.client("ssm")
    response = ssm.get_parameter(Name=name, WithDecryption=decrypt)
    return response["Parameter"]["Value"]
