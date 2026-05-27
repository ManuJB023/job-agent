#!/usr/bin/env python3
"""Bootstrap helper: probe a list of candidate Greenhouse slugs and report
which ones have public boards with active jobs.

Usage:
    python3 scripts/seed_target_companies.py slug1 slug2 slug3 ...

Or pipe a file:
    cat candidates.txt | python3 scripts/seed_target_companies.py

Output is a YAML-ready list of confirmed entries that can be pasted into
config/config.yaml under `target_companies`.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def probe_greenhouse(slug: str) -> int | None:
    """Returns job count, or None if the board doesn't exist."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            return len(data.get("jobs", []))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception as e:
        print(f"  ! {slug}: {e}", file=sys.stderr)
        return None


def main() -> int:
    if not sys.stdin.isatty():
        candidates = [line.strip() for line in sys.stdin if line.strip()]
    else:
        candidates = sys.argv[1:]

    if not candidates:
        print(__doc__)
        return 1

    print("# Confirmed Greenhouse boards (paste under target_companies in config.yaml):")
    for slug in candidates:
        count = probe_greenhouse(slug)
        if count is None:
            print(f"#   skip {slug} - no public board found", file=sys.stderr)
        elif count == 0:
            print(f"#   skip {slug} - board exists but 0 open jobs", file=sys.stderr)
        else:
            print(f"  - {{ ats: greenhouse, slug: {slug} }}    # {count} open jobs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
