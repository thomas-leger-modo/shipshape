# ruff: noqa: T201
"""Data layer for update-tickets; no terminal UI here."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from typing import NamedTuple

import requests

from shipshape.config import USER_ID_FILE, notion_data_source_id, notion_token, notion_user_id

NOTION_VERSION = "2025-09-03"
READY_FOR_REVIEW_LABEL = "Ready for Review"
# Finished tickets are never re-checked — skip them at the query so we don't fetch/gh-probe them.
TERMINAL_STATUSES = ("Released", "Cancelled")
PR_URL_PATTERN = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")


class PRRef(NamedTuple):
    owner: str
    repo: str
    number: int


class ProdDeployment(NamedTuple):
    sha: str
    deployed_at: datetime
    url: str | None


def _notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {notion_token()}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


# --- Notion reads ---------------------------------------------------------------------------


def query_my_tickets(user_id: str) -> list[dict]:
    headers = _notion_headers()
    data_source_id = notion_data_source_id()
    body: dict = {
        "filter": {
            "and": [
                {"property": "Assignee", "people": {"contains": user_id}},
                *({"property": "Status", "status": {"does_not_equal": status}} for status in TERMINAL_STATUSES),
            ],
        },
        "sorts": [{"property": "Created time", "direction": "descending"}],
        "page_size": 100,
    }
    pages: list[dict] = []
    cursor: str | None = None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/data_sources/{data_source_id}/query",
            headers=headers,
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        pages.extend(d["results"])
        if not d.get("has_more"):
            return pages
        cursor = d["next_cursor"]


def parse_ticket(page: dict) -> dict:
    props = page["properties"]
    title = "".join(c.get("plain_text", "") for c in (props.get("Task name", {}).get("title") or []))
    tid = props.get("Task ID", {}).get("unique_id") or {}
    men = f"{tid.get('prefix', '')}-{tid.get('number', '')}"
    status = (props.get("Status", {}).get("status") or {}).get("name")
    # Each PR is a Notion link (URL on `href`, title on `plain_text`); scan both so pasted URLs count.
    pr_text = "".join(
        (c.get("href") or "") + " " + (c.get("plain_text") or "") for c in (props.get("PRs", {}).get("rich_text") or [])
    )
    matches = PR_URL_PATTERN.findall(pr_text)
    pr_refs = sorted({PRRef(owner=owner, repo=repo, number=int(n)) for owner, repo, n in matches})
    return {"page_id": page["id"], "url": page.get("url"), "men": men, "title": title, "status": status, "pr_refs": pr_refs}


# --- GitHub reads ---------------------------------------------------------------------------


def get_prod_deployments(owner: str, repo: str, limit: int = 30) -> list[ProdDeployment]:
    """Successful `prod` deployments, de-duplicated by SHA, oldest first. GraphQL avoids N+1 REST."""
    query = """
    query($owner:String!, $name:String!, $limit:Int!) {
      repository(owner:$owner, name:$name) {
        deployments(first: $limit, environments: ["prod"], orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes { commitOid statuses(first: 20) { nodes { state createdAt logUrl } } }
        }
      }
    }
    """
    r = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={repo}", "-F", f"limit={limit}"],
        capture_output=True,
        text=True,
        check=True,
    )
    nodes = json.loads(r.stdout)["data"]["repository"]["deployments"]["nodes"]
    first_by_sha: dict[str, ProdDeployment] = {}
    for node in nodes:
        successes = [s for s in node["statuses"]["nodes"] if s["state"] == "SUCCESS"]
        if not successes:
            continue
        first = min(successes, key=lambda s: s["createdAt"])
        candidate = ProdDeployment(
            sha=node["commitOid"],
            deployed_at=datetime.fromisoformat(first["createdAt"].replace("Z", "+00:00")),
            url=first["logUrl"],
        )
        existing = first_by_sha.get(candidate.sha)
        if existing is None or candidate.deployed_at < existing.deployed_at:
            first_by_sha[candidate.sha] = candidate
    return sorted(first_by_sha.values(), key=lambda d: d.deployed_at)


def _checks_passing(rollup: list[dict]) -> bool:
    """True only when there are checks and every one completed successfully."""
    if not rollup:
        return False
    for check in rollup:
        if check.get("status") and check["status"] != "COMPLETED":
            return False
        if (check.get("conclusion") or check.get("state")) not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            return False
    return True


def gh_pr_state(ref: PRRef) -> dict:
    r = subprocess.run(
        [
            "gh", "pr", "view", str(ref.number),
            "--repo", f"{ref.owner}/{ref.repo}",
            "--json", "state,mergeCommit,number,title,url,labels,statusCheckRollup",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return {"owner": ref.owner, "repo": ref.repo, "number": ref.number, "state": "ERROR", "error": r.stderr.strip()}
    d = json.loads(r.stdout)
    labels = sorted(label["name"] for label in d["labels"])
    return {
        "owner": ref.owner,
        "repo": ref.repo,
        "number": d["number"],
        "title": d["title"],
        "url": d["url"],
        "state": d["state"],
        "merge_sha": (d.get("mergeCommit") or {}).get("oid"),
        "labels": labels,
        "ready_for_review": READY_FOR_REVIEW_LABEL in labels,
        "checks_passing": _checks_passing(d["statusCheckRollup"]),
        "checks": d["statusCheckRollup"],
    }


def newest_containing_deployment(owner: str, repo: str, merge_sha: str | None, deployments: list[ProdDeployment]) -> ProdDeployment | None:
    """Newest prod deployment whose SHA has `merge_sha` as an ancestor. `deployments` is oldest-first.

    `compare/{base}...{head}` gives head relative to base: 'ahead'/'identical' means base is an
    ancestor of head (merge commit is in that deploy); 'behind'/'diverged' means it is not.
    """
    if not merge_sha:
        return None
    for deployment in reversed(deployments):
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/compare/{merge_sha}...{deployment.sha}", "--jq", ".status"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"compare API failed for {owner}/{repo}: {r.stderr.strip()[:200]}")
        match r.stdout.strip():
            case "ahead" | "identical":
                return deployment
            case "behind" | "diverged":
                continue
            case other:
                raise RuntimeError(f"unrecognized compare status {other!r} for {owner}/{repo}")
    return None


# --- Proposal -------------------------------------------------------------------------------


def propose_status(current: str, prs: list[dict]) -> str | None:
    """Target status from PR state, or None to leave the ticket alone.

    A PR lookup ERROR leaves the ticket alone: reasoning about a partial PR set could promote a
    ticket whose unfetched PR is actually still open. No PRs, or any PR closed-without-merge, also
    leaves it alone. Otherwise PR state drives the status regardless of the current value.
    """
    if not prs or any(p["state"] == "ERROR" for p in prs):
        return None
    valid = [p for p in prs if p["state"] in ("OPEN", "MERGED", "CLOSED")]
    if not valid:
        return None
    open_prs = [p for p in valid if p["state"] == "OPEN"]
    if open_prs:
        proposed = "In Review" if all(p["ready_for_review"] for p in open_prs) else "In Progress"
    elif {p["state"] for p in valid} == {"MERGED"}:
        proposed = "Released" if any(p["in_prod"] for p in valid) else "Ready for Release"
    else:
        return None
    return proposed if proposed != current else None


def collect_tickets() -> list[dict]:
    """One record per ticket: current + proposed status, and its PRs with deploy state resolved."""
    tickets = [parse_ticket(page) for page in query_my_tickets(notion_user_id())]

    deployments_by_repo = {
        (ref.owner, ref.repo): get_prod_deployments(ref.owner, ref.repo)
        for ticket in tickets
        for ref in ticket["pr_refs"]
    }

    out: list[dict] = []
    for ticket in tickets:
        prs = [gh_pr_state(ref) for ref in ticket["pr_refs"]]
        for pr in prs:
            deployment = None
            if pr["state"] == "MERGED" and pr.get("merge_sha"):
                deployment = newest_containing_deployment(
                    pr["owner"], pr["repo"], pr["merge_sha"], deployments_by_repo[(pr["owner"], pr["repo"])],
                )
            pr["prod_sha"] = deployment.sha if deployment else None
            pr["prod_deployed_at"] = deployment.deployed_at if deployment else None
            pr["prod_deployment_url"] = deployment.url if deployment else None
            pr["in_prod"] = deployment is not None
        out.append(
            {
                "page_id": ticket["page_id"],
                "url": ticket["url"],
                "men": ticket["men"],
                "title": ticket["title"],
                "current": ticket["status"],
                "proposed": propose_status(ticket["status"], prs),
                "prs": prs,
            },
        )
    return out


# --- Writes ---------------------------------------------------------------------------------


def set_status(page_id: str, status: str) -> None:
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=_notion_headers(),
        json={"properties": {"Status": {"status": {"name": status}}}},
        timeout=30,
    )
    r.raise_for_status()


def add_ready_label(owner: str, repo: str, number: int) -> None:
    subprocess.run(
        ["gh", "pr", "edit", str(number), "--repo", f"{owner}/{repo}", "--add-label", READY_FOR_REVIEW_LABEL],
        check=True,
    )


# --- Setup ----------------------------------------------------------------------------------


def resolve_and_store_user_id(email: str | None = None) -> None:
    if not email:
        r = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, check=False)
        if r.returncode != 0 or not r.stdout.strip():
            sys.exit("Could not read `git config user.email`. Pass an email argument.")
        email = r.stdout.strip()
    email = email.lower()

    headers = _notion_headers()
    cursor: str | None = None
    while True:
        params = {"page_size": 100} | ({"start_cursor": cursor} if cursor else {})
        r = requests.get("https://api.notion.com/v1/users", headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for user in data.get("results", []):
            if (user.get("person") or {}).get("email", "").lower() == email:
                USER_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
                USER_ID_FILE.write_text(user["id"] + "\n")
                print(f"Resolved {email} -> {user.get('name', '')} ({user['id']})")
                print(f"Saved to {USER_ID_FILE}")
                return
        if not data.get("has_more"):
            sys.exit(f"No Notion user found with email {email}. Are you in the right workspace?")
        cursor = data.get("next_cursor")
