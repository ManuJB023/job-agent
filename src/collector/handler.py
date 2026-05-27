"""Collector Lambda.

Runs on a schedule. Ingests job postings from three sources concurrently:
  1. JobSpy   — LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs.
  2. JSearch  — RapidAPI aggregator over Google for Jobs. Fallback when
                 JobSpy hits rate limits.
  3. Greenhouse — direct ATS JSON for target companies. Highest signal.

Each source is wrapped to never raise — a failure in one source doesn't
break the others. All postings flow into the same `JobPosting` shape and
are deduplicated by content hash at DynamoDB write time.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
from pathlib import Path
from typing import Any

import yaml

from shared import (
    JobPosting,
    configure_logging,
    get_ssm_parameter,
    get_table,
    log_json,
    put_job_if_new,
)

log = configure_logging()

# ─────────────────────────────────────────────────────────────────────────────
# Config loading
# ─────────────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Source 1 — JobSpy
# ─────────────────────────────────────────────────────────────────────────────
def collect_jobspy(search_terms: list[str], location: str, hours_old: int, results_per_term: int) -> list[JobPosting]:
    """Pull from LinkedIn/Indeed/Glassdoor/ZipRecruiter via JobSpy.

    JobSpy is lazy-imported because it pulls in pandas and is the heaviest
    dependency — keep cold starts fast when other sources still work.
    """
    try:
        from jobspy import scrape_jobs  # type: ignore
    except ImportError:
        log_json(log, "warning", "jobspy_not_installed")
        return []

    out: list[JobPosting] = []
    for term in search_terms:
        try:
            df = scrape_jobs(
                site_name=["indeed", "linkedin", "zip_recruiter", "glassdoor", "google"],
                search_term=term,
                google_search_term=f"{term} jobs near {location}",
                location=location,
                results_wanted=results_per_term,
                hours_old=hours_old,
                country_indeed="USA",
                # linkedin_fetch_description=True,  # slower but richer; enable when stable
            )
        except Exception as e:
            log_json(log, "warning", "jobspy_term_failed", term=term, error=str(e))
            continue

        for _, row in df.iterrows():
            site = str(row.get("site", "unknown"))
            try:
                out.append(JobPosting(
                    company=str(row.get("company") or "Unknown"),
                    title=str(row.get("title") or "Unknown"),
                    location=str(row.get("location") or location),
                    description=str(row.get("description") or ""),
                    apply_url=str(row.get("job_url") or ""),
                    source=f"jobspy:{site}",
                    remote=bool(row.get("is_remote")),
                    posted_at=str(row.get("date_posted") or "") or None,
                    salary_min=_safe_int(row.get("min_amount")),
                    salary_max=_safe_int(row.get("max_amount")),
                ))
            except Exception as e:
                log_json(log, "warning", "jobspy_row_skipped", error=str(e))

    log_json(log, "info", "jobspy_done", count=len(out))
    return out


def _safe_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Source 2 — JSearch (RapidAPI)
# ─────────────────────────────────────────────────────────────────────────────
def collect_jsearch(search_terms: list[str], location: str, hours_old: int) -> list[JobPosting]:
    api_key_param = os.environ.get("JSEARCH_PARAM", "/job-agent/jsearch_api_key")
    try:
        api_key = get_ssm_parameter(api_key_param)
    except Exception as e:
        log_json(log, "info", "jsearch_no_key", reason=str(e))
        return []

    import urllib.parse
    import urllib.request

    out: list[JobPosting] = []
    date_posted = "3days" if hours_old <= 72 else "week"

    for term in search_terms:
        query = urllib.parse.quote_plus(f"{term} in {location}")
        url = (
            "https://jsearch.p.rapidapi.com/search"
            f"?query={query}&page=1&num_pages=2&date_posted={date_posted}"
        )
        req = urllib.request.Request(url, headers={
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": "jsearch.p.rapidapi.com",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                import json as _json
                data = _json.loads(resp.read())
        except Exception as e:
            log_json(log, "warning", "jsearch_term_failed", term=term, error=str(e))
            continue

        for j in data.get("data", []):
            try:
                out.append(JobPosting(
                    company=j.get("employer_name") or "Unknown",
                    title=j.get("job_title") or "Unknown",
                    location=_assemble_location(j),
                    description=j.get("job_description") or "",
                    apply_url=j.get("job_apply_link") or j.get("job_google_link") or "",
                    source="jsearch",
                    remote=bool(j.get("job_is_remote")),
                    posted_at=j.get("job_posted_at_datetime_utc"),
                    salary_min=_safe_int(j.get("job_min_salary")),
                    salary_max=_safe_int(j.get("job_max_salary")),
                ))
            except Exception as e:
                log_json(log, "warning", "jsearch_row_skipped", error=str(e))

    log_json(log, "info", "jsearch_done", count=len(out))
    return out


def _assemble_location(j: dict) -> str:
    parts = [j.get("job_city"), j.get("job_state"), j.get("job_country")]
    return ", ".join(p for p in parts if p) or "Unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Source 3 — Greenhouse / Lever / Ashby direct ATS endpoints
# ─────────────────────────────────────────────────────────────────────────────
def collect_ats(target_companies: list[dict]) -> list[JobPosting]:
    """Hit public ATS endpoints for a curated list of target companies.

    Each entry in target_companies is:
        {ats: "greenhouse"|"lever"|"ashby", slug: "<companyslug>"}
    """
    import urllib.request
    import json as _json
    out: list[JobPosting] = []

    for entry in target_companies:
        ats = entry["ats"]
        slug = entry["slug"]
        try:
            if ats == "greenhouse":
                url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
            elif ats == "lever":
                url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
            elif ats == "ashby":
                url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
            else:
                continue

            with urllib.request.urlopen(url, timeout=20) as resp:
                data = _json.loads(resp.read())
        except Exception as e:
            log_json(log, "warning", "ats_company_failed", company=slug, ats=ats, error=str(e))
            continue

        out.extend(_parse_ats_response(ats, slug, data))

    log_json(log, "info", "ats_done", count=len(out), companies=len(target_companies))
    return out


def _parse_ats_response(ats: str, slug: str, data: Any) -> list[JobPosting]:
    out: list[JobPosting] = []

    if ats == "greenhouse":
        for j in data.get("jobs", []):
            location_str = (j.get("location") or {}).get("name", "Unknown")
            out.append(JobPosting(
                company=slug.replace("-", " ").title(),
                title=j.get("title", "Unknown"),
                location=location_str,
                description=_strip_html(j.get("content", "")),
                apply_url=j.get("absolute_url", ""),
                source=f"greenhouse:{slug}",
                remote="remote" in location_str.lower(),
                posted_at=j.get("updated_at"),
            ))
    elif ats == "lever":
        for j in data:
            categories = j.get("categories", {})
            location = categories.get("location", "Unknown")
            out.append(JobPosting(
                company=slug.replace("-", " ").title(),
                title=j.get("text", "Unknown"),
                location=location,
                description=_strip_html(j.get("descriptionPlain") or j.get("description", "")),
                apply_url=j.get("hostedUrl", ""),
                source=f"lever:{slug}",
                remote="remote" in location.lower(),
                posted_at=str(j.get("createdAt", "")),
            ))
    elif ats == "ashby":
        for j in data.get("jobs", []):
            location = j.get("locationName", "Unknown")
            out.append(JobPosting(
                company=slug.replace("-", " ").title(),
                title=j.get("title", "Unknown"),
                location=location,
                description=_strip_html(j.get("descriptionHtml") or j.get("descriptionPlain", "")),
                apply_url=j.get("jobUrl", ""),
                source=f"ashby:{slug}",
                remote=bool(j.get("isRemote")),
                posted_at=j.get("publishedAt"),
            ))

    return out


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _HTML_TAG_RE.sub("", s).replace("&nbsp;", " ").strip()


# ─────────────────────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────────────────────
def handler(event, context):
    config = load_config()
    search_terms = config["search_terms"]
    location = config["location"]
    hours_old = config.get("hours_old", 24)
    results_per_term = config.get("results_per_term", 25)
    target_companies = config.get("target_companies", [])

    log_json(log, "info", "collector_start",
             terms=len(search_terms), companies=len(target_companies))

    # Run all three sources in parallel. Each is wrapped so one failure
    # doesn't propagate.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_jobspy = pool.submit(_safe, collect_jobspy, search_terms, location, hours_old, results_per_term)
        f_jsearch = pool.submit(_safe, collect_jsearch, search_terms, location, hours_old)
        f_ats = pool.submit(_safe, collect_ats, target_companies)
        all_postings = f_jobspy.result() + f_jsearch.result() + f_ats.result()

    # Filter to postings with the minimum viable data.
    valid = [p for p in all_postings if p.title and p.company and p.description]
    log_json(log, "info", "collector_normalized", total=len(all_postings), valid=len(valid))

    # Write each to DynamoDB with conditional put. Counts new vs duplicate.
    table = get_table()
    new_count = 0
    dup_count = 0
    for posting in valid:
        try:
            if put_job_if_new(table, posting):
                new_count += 1
            else:
                dup_count += 1
        except Exception as e:
            log_json(log, "error", "put_failed",
                     company=posting.company, title=posting.title, error=str(e))

    log_json(log, "info", "collector_done", new=new_count, duplicate=dup_count)
    return {"new": new_count, "duplicate": dup_count, "total_seen": len(valid)}


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log_json(log, "error", "source_crashed", fn=fn.__name__, error=str(e))
        return []
