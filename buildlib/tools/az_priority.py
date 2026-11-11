#!/usr/bin/env python3
#
# Copyright (c) NVIDIA CORPORATION & AFFILIATES, 2026. ALL RIGHTS RESERVED.
# See file LICENSE for terms.
#
# /// script
# requires-python = ">=3.8"
# dependencies = ["requests"]
# ///

"""
Prioritize Azure DevOps build jobs.

Bumps pending jobs of a build to the front of pool 11's queue so they get
picked up by agents sooner. Optionally polls until the build finishes.

Usage:
  az_priority.py --scan                     # Builds of open PRs labeled --label
  az_priority.py --scan --label '<name>'
  az_priority.py --scan --title-pattern '<regex>'
                                         # Also PR builds whose PR title matches
  az_priority.py --scan --loop --duration 55
                                            # Rescan every --interval for 55 minutes
  az_priority.py --scan --dry-run           # List what would be prioritized
  az_priority.py <build_id>                 # One-time run
  az_priority.py --loop <build_id>          # Monitor until complete
  az_priority.py --loop --interval 60 <id>  # Poll every 60s
  az_priority.py <id> <id> ...              # Several builds
  az_priority.py --pr <number> [<number>..] # All unfinished builds of UCX PR(s)
  az_priority.py --loop --pr <n> <n>        # Monitor UCX PRs until their builds finish
  az_priority.py <id> --pr <number>         # Builds and PRs can be mixed

Needs `requests`: install it, or run with `uv run` (reads the dependency
block above).

Exits non-zero when an API call failed.

GITHUB_TOKEN env var is optional: the label lookup works without it, within
GitHub's lower rate limit for anonymous requests.

Token: AZURE_DEVOPS_PAT env var - a PAT with Manage permission on the pool.
"""

import argparse
import os
import re
import sys
import time
from base64 import b64encode
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("Missing 'requests': run with 'uv run az_priority.py' or 'pip install requests'")


API_VERSION = "7.0"
BASE_URL = "https://dev.azure.com/ucfconsort"
PROJECT = "ucx"
GITHUB_REPO = "openucx/ucx"
DEFAULT_LABEL = "CI priority"


def get_pat():
    """Read the Azure DevOps token from AZURE_DEVOPS_PAT."""
    pat = os.environ.get("AZURE_DEVOPS_PAT", "").strip()
    if not pat:
        raise ValueError("AZURE_DEVOPS_PAT is not set")
    return b64encode(f":{pat}".encode()).decode()


def get_headers(pat_b64):
    return {
        "Content-Type": "application/json",
        "Authorization": f"Basic {pat_b64}",
    }


def check_pat(headers):
    """Fail fast if the PAT is invalid or missing permissions."""
    response = requests.get(BASE_URL, headers=headers, timeout=30)
    if response.status_code == 401:
        raise PermissionError("Authentication failed (401). Check your PAT and scopes.")


api_errors = 0


def api_request(url, headers, method="GET", json_data=None):
    """Make API request with error handling."""
    global api_errors
    try:
        if method == "PATCH":
            response = requests.patch(url, headers=headers, json=json_data, timeout=30)
        else:
            response = requests.get(url, headers=headers, timeout=30)

        response.raise_for_status()
        return response.json() if method == "GET" else response
    except requests.RequestException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        body = getattr(getattr(e, "response", None), "text", "")
        details = f" (status={status})" if status is not None else ""
        print(f"API error{details}: {e}")
        if body:
            print(f"Response body: {body[:2000]}")
        api_errors += 1
        return None


def get_pr_builds(pr_number, headers):
    """Return IDs of unfinished builds for the latest commit of a UCX PR.

    A PR push queues several pipelines (UCX PR, snapshot, release, ...) on
    refs/pull/<N>/merge. Builds of older commits are skipped.
    """
    url = (
        f"{BASE_URL}/{PROJECT}/_apis/build/builds"
        f"?branchName=refs/pull/{pr_number}/merge"
        f"&queryOrder=queueTimeDescending&$top=20&api-version={API_VERSION}"
    )
    data = api_request(url, headers)
    if data is None:
        return None

    builds = data.get("value", [])
    if not builds:
        print(f"No builds found for PR {pr_number}")
        return []

    latest_sha = builds[0].get("sourceVersion")
    build_ids = [
        b["id"]
        for b in builds
        if b.get("sourceVersion") == latest_sha and b.get("status") != "completed"
    ]
    print(f"PR {pr_number} @ {latest_sha[:12]}: unfinished builds {build_ids or 'none'}")
    return build_ids


def get_labeled_prs(label):
    """Return the numbers of open GITHUB_REPO PRs that carry the label."""
    gh_headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        gh_headers["Authorization"] = f"Bearer {token}"

    pr_numbers = set()
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{GITHUB_REPO}/issues"
            f"?labels={quote(label)}&state=open&per_page=100&page={page}"
        )
        issues = api_request(url, gh_headers)
        if issues is None:
            return None
        pr_numbers.update(str(i["number"]) for i in issues if "pull_request" in i)
        if len(issues) < 100:
            return pr_numbers
        page += 1


def scan_pr_builds(headers, label, title_regex):
    """Return IDs of unfinished PR builds selected by PR label or PR title.

    A build is selected when its PR carries the label, or when title_regex is
    set and matches the PR title. Azure records the title in triggerInfo when
    the build is queued, so a title edit takes effect on the next push; a
    label is read from GitHub on every scan.
    """
    labeled_prs = set()
    if label:
        labeled_prs = get_labeled_prs(label)
        if labeled_prs is None:
            return None
        print(f"Open PRs labeled '{label}': {sorted(labeled_prs, key=int) or 'none'}")

    url = (
        f"{BASE_URL}/{PROJECT}/_apis/build/builds"
        f"?reasonFilter=pullRequest&statusFilter=inProgress,notStarted"
        f"&api-version={API_VERSION}"
    )
    data = api_request(url, headers)
    if data is None:
        return None

    builds = data.get("value", [])
    build_ids = []
    for b in builds:
        info = b.get("triggerInfo") or {}
        if info.get("pr.number") in labeled_prs:
            reason = f"label '{label}'"
        elif title_regex and title_regex.search(info.get("pr.title", "")):
            reason = f"title '{info.get('pr.title')}'"
        else:
            continue
        print(f"Match: build {b['id']} ({b['definition']['name']}) "
              f"PR {info.get('pr.number')} by {reason}")
        build_ids.append(b["id"])
    print(f"Scan: {len(build_ids)}/{len(builds)} unfinished PR builds match")
    return build_ids


def get_build_info(build_id, headers):
    """Get build status and job details with pool information."""
    base_url = f"{BASE_URL}/{PROJECT}/_apis/build/builds/{build_id}"

    # Get build data
    build_data = api_request(f"{base_url}?api-version={API_VERSION}", headers)
    if not build_data:
        return None, None, []

    status = build_data.get("status", "unknown")
    plan_id = (build_data.get("orchestrationPlan") or {}).get("planId")

    # Get timeline with job details
    timeline_data = api_request(f"{base_url}/timeline?api-version={API_VERSION}", headers)
    if not timeline_data:
        return status, plan_id, []

    # Extract pending jobs with metadata
    pending_jobs = []
    for record in timeline_data.get("records", []):
        if (
            record.get("state") == "pending"
            and record.get("type") == "Job"
            and record.get("id")
        ):
            pending_jobs.append(
                {
                    "job_id": record["id"],
                    "name": record.get("name", "Unknown"),
                }
            )

    print(f"Build {build_id}: {status}, {len(pending_jobs)} pending jobs")
    return status, plan_id, pending_jobs


def find_job_requests(plan_id, job_data, headers):
    """Find pool 11 request IDs for this build's pending jobs.

    Timeline job IDs are derived from the job name, so every build of the
    same pipeline shares them. The request's planId is what ties it to one
    build, so match on both.
    """
    if not job_data:
        return []

    # Only query the single required pool
    pool_id = 11
    # completedRequestCount=0 returns only unfinished requests instead of the
    # pool's full history (~18 MB), which can exceed the read timeout
    url = (
        f"{BASE_URL}/_apis/distributedtask/pools/{pool_id}/jobrequests"
        f"?completedRequestCount=0&api-version={API_VERSION}"
    )
    requests_data = api_request(url, headers)
    if not requests_data or "value" not in requests_data:
        print("No job requests found in pool 11")
        return []

    job_lookup = {job["job_id"]: job for job in job_data}
    matched_requests = []

    for request in requests_data["value"]:
        if request.get("planId") != plan_id:
            continue

        job_id = request.get("jobId")
        if not job_id or job_id not in job_lookup:
            continue

        # Skip requests already assigned to an agent - reprioritizing has no effect
        if request.get("assignTime") or request.get("reservedAgent"):
            continue

        if request.get("finishTime") or request.get("result") not in [None, "pending"]:
            continue

        matched_requests.append(
            {
                "request_id": request["requestId"],
                "job_id": job_id,
                "job_name": job_lookup[job_id]["name"],
                "pool_id": pool_id,
            }
        )

    print(f"Found {len(matched_requests)} valid requests for {len(job_data)} jobs")
    return matched_requests


def prioritize_requests(requests, headers, dry_run=False):
    """Prioritize job requests."""
    if not requests:
        return

    if dry_run:
        for req_data in requests:
            print(f"Dry run - would prioritize: {req_data['job_name']}")
        return

    success_count = 0
    for i, req_data in enumerate(requests, 1):
        req_id = req_data["request_id"]
        pool_id = req_data["pool_id"]
        job_name = req_data["job_name"]

        print(f"Prioritizing {i}/{len(requests)}: {job_name}")

        url = (
            f"{BASE_URL}/_apis/distributedtask/pools/{pool_id}"
            f"/jobrequests/{req_id}?lockToken=00000000-0000-0000-0000-000000000000"
            f"&updateOptions=1&api-version={API_VERSION}"
        )

        response = api_request(url, headers, "PATCH", {"requestId": req_id})
        if response:
            success_count += 1
        else:
            print(f"  Failed to prioritize {req_id}")

    print(f"Successfully prioritized {success_count}/{len(requests)} requests")


def process_build(build_id, headers, dry_run=False):
    """Process one build - check status and prioritize pending jobs."""
    status, plan_id, job_data = get_build_info(build_id, headers)

    if not status:
        print("Failed to get build info")
        return None

    if not plan_id:
        print("Build has no orchestration plan id - cannot match job requests")
        return status

    if status in ["completed", "cancelled", "failed"]:
        return status

    if not job_data:
        print("No pending jobs - nothing to prioritize")
        return status

    requests = find_job_requests(plan_id, job_data, headers)
    prioritize_requests(requests, headers, dry_run)
    return status


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Prioritize pending Azure DevOps build jobs in pool 11.",
        epilog="Token: AZURE_DEVOPS_PAT env var.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "build_ids",
        nargs="*",
        metavar="build_id",
        help="Azure DevOps build ID(s) to prioritize",
    )
    parser.add_argument(
        "--pr",
        type=int,
        nargs="+",
        default=[],
        metavar="NUMBER",
        help="UCX GitHub PR number(s) - prioritize all unfinished builds of each PR's latest commit",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Prioritize unfinished PR builds selected by --label or --title-pattern",
    )
    parser.add_argument(
        "--label",
        default=DEFAULT_LABEL,
        metavar="az_priority.py",
        help=f"GitHub PR label for --scan (default: '{DEFAULT_LABEL}', '' to skip)",
    )
    parser.add_argument(
        "--title-pattern",
        metavar="REGEX",
        help="PR title regex for --scan, in addition to --label",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Monitor the targets, reprioritizing until all finish (with --scan: until stopped)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Poll interval in --loop mode (default: 300)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        metavar="MINUTES",
        help="Stop --loop after this many minutes (default: no limit)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be prioritized without changing the queue",
    )
    args = parser.parse_args(argv)
    if not args.build_ids and not args.pr and not args.scan:
        parser.error("give at least one build_id, --pr NUMBER or --scan")
    if args.scan and not args.label and not args.title_pattern:
        parser.error("--scan needs --label or --title-pattern")
    try:
        args.title_regex = re.compile(args.title_pattern) if args.title_pattern else None
    except re.error as e:
        parser.error(f"bad --title-pattern: {e}")
    return args


if __name__ == "__main__":
    args = parse_args()

    try:
        headers = get_headers(get_pat())
        check_pat(headers)
    except Exception as e:
        print(e)
        sys.exit(1)

    # Targets still being watched; finished ones are dropped each pass
    pending_builds = list(dict.fromkeys(args.build_ids))
    pending_prs = list(dict.fromkeys(args.pr))

    def run_once():
        """Process every target once. Returns True when there is nothing left to watch."""
        # A build listed directly can also belong to a listed PR; process it once per pass
        done = set()
        for build_id in list(pending_builds):
            done.add(str(build_id))
            status = process_build(build_id, headers, args.dry_run)
            if not status:
                print(f"Build {build_id} status unknown, continuing...")
            elif status in ["completed", "cancelled", "failed"]:
                print(f"Build {build_id} finished: {status}")
                pending_builds.remove(build_id)

        for pr in list(pending_prs):
            # Re-resolve every pass so a new push to the PR is picked up
            build_ids = get_pr_builds(pr, headers)
            if build_ids is None:
                print(f"Failed to list PR {pr} builds, continuing...")
                continue
            for build_id in build_ids:
                if str(build_id) not in done:
                    done.add(str(build_id))
                    process_build(build_id, headers, args.dry_run)
            if not build_ids:
                print(f"PR {pr}: all builds finished")
                pending_prs.remove(pr)

        if args.scan:
            build_ids = scan_pr_builds(headers, args.label, args.title_regex)
            if build_ids is None:
                print("Failed to scan PR builds, continuing...")
            for build_id in build_ids or []:
                if str(build_id) not in done:
                    done.add(str(build_id))
                    process_build(build_id, headers, args.dry_run)
            # A scan never runs out of targets; --loop keeps going until stopped
            return False

        return not pending_builds and not pending_prs

    targets = [f"build {b}" for b in pending_builds] + [f"PR {p}" for p in pending_prs]
    if args.scan:
        if args.label:
            targets.append(f"PRs labeled '{args.label}'")
        if args.title_pattern:
            targets.append(f"PR titles matching '{args.title_pattern}'")
    if args.loop:
        print(f"Monitoring {', '.join(targets)} every {args.interval}s...")
        deadline = time.monotonic() + args.duration * 60 if args.duration else None
        try:
            while True:
                print(f'\n--- {time.strftime("%H:%M:%S")} ---')
                if run_once():
                    break
                if deadline and time.monotonic() + args.interval > deadline:
                    print(f"Stopping after {args.duration:g} minutes")
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped")
    else:
        run_once()

    if api_errors:
        print(f"{api_errors} API call(s) failed")
        sys.exit(1)
