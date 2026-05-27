"""Tests for shared models and helpers."""

from __future__ import annotations

import sys
from pathlib import Path

# Make src/ importable from tests/
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from shared import JobPosting, ScoreResult


def test_content_hash_is_deterministic():
    j1 = JobPosting(
        company="SLB",
        title="Cloud Platform Engineer",
        location="Houston, TX",
        description="Build cloud infra for SLB digital initiatives.",
        apply_url="https://example.com/job/1",
        source="jobspy:linkedin",
    )
    j2 = JobPosting(
        company="SLB",
        title="Cloud Platform Engineer",
        location="Houston, TX",
        description="Build cloud infra for SLB digital initiatives.",
        apply_url="https://different-url.com/job/99",  # different URL — still dedupes
        source="jobspy:indeed",
    )
    assert j1.content_hash() == j2.content_hash()


def test_content_hash_case_and_whitespace_insensitive():
    a = JobPosting(
        company="  HashiCorp  ",
        title="DevOps Engineer",
        location="Remote",
        description="Terraform team.",
        apply_url="x",
        source="x",
    )
    b = JobPosting(
        company="hashicorp",
        title="devops engineer",
        location="REMOTE",
        description="terraform team.",
        apply_url="y",
        source="y",
    )
    assert a.content_hash() == b.content_hash()


def test_to_dynamo_item_truncates_long_description():
    huge_desc = "x" * 100_000
    j = JobPosting(
        company="Acme",
        title="Engineer",
        location="Remote",
        description=huge_desc,
        apply_url="https://example.com",
        source="test",
    )
    item = j.to_dynamo_item()
    assert len(item["description"]) <= 30_000
    assert item["status"] == "PENDING"
    assert item["pk"].startswith("JOB#")
    assert item["sk"] == "META"


def test_score_result_validation_rejects_out_of_range():
    bad = ScoreResult(fit_score=150, best_resume_variant="cloud_devops")
    try:
        bad.validate()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_score_result_validation_rejects_unknown_variant():
    bad = ScoreResult(fit_score=80, best_resume_variant="data_science")
    try:
        bad.validate()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_score_result_validation_accepts_valid():
    good = ScoreResult(
        fit_score=85,
        best_resume_variant="cloud_devops",
        matched_skills=["AWS", "Terraform"],
        gaps=["Kubernetes"],
        reasoning="Strong AWS + IaC fit; missing K8s mentioned as plus.",
    )
    good.validate()  # should not raise


def test_score_result_validation_accepts_industrial_iot():
    good = ScoreResult(
        fit_score=92,
        best_resume_variant="industrial_iot",
        matched_skills=["AWS", "MQTT", "SCADA", "field operations"],
        gaps=[],
        reasoning="Direct OT-to-cloud fit; 15+ yrs sensor telemetry maps to industrial IoT ingest patterns.",
    )
    good.validate()  # should not raise
