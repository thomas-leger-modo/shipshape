# ruff: noqa: T201
"""Interactively review and apply deterministic ticket/PR status proposals."""

from __future__ import annotations

import json
import shlex
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from shipshape.tickets import engine
from shipshape.ui import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW, confirm, get_preview_height, require, run_fzf


class ProductionConfirmation(NamedTuple):
    confirmed_prs: int
    total_prs: int
    latest_at: datetime | None


def _datetime_json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def get_ready_pr_candidates(tickets: list[dict]) -> list[dict]:
    candidates_by_pr: dict[tuple[str, str, int], dict] = {}
    for ticket in tickets:
        for pr in ticket["prs"]:
            if pr["state"] != "OPEN" or not pr["checks_passing"] or pr["ready_for_review"]:
                continue
            key = (pr["owner"], pr["repo"], pr["number"])
            candidate = candidates_by_pr.setdefault(key, {"pr": pr, "tickets": []})
            candidate["tickets"].append(
                {"men": ticket["men"], "title": ticket["title"], "current": ticket["current"], "proposed": ticket["proposed"]},
            )
    return [candidates_by_pr[key] for key in sorted(candidates_by_pr)]


def get_status_candidates(tickets: list[dict]) -> list[dict]:
    return [ticket for ticket in tickets if ticket["proposed"] is not None]


def _short_sha(sha: str | None) -> str:
    return sha[:12] if sha else "—"


def _terminal_link(label: str, url: str) -> str:
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


def _format_deployed_at(deployed_at: datetime) -> str:
    return deployed_at.astimezone().strftime("%d %b %Y at %H:%M %Z").removeprefix("0")


def get_ticket_production_confirmation(ticket: dict) -> ProductionConfirmation:
    deployment_times = [pr["prod_deployed_at"] for pr in ticket["prs"] if pr.get("prod_deployed_at")]
    return ProductionConfirmation(
        confirmed_prs=len(deployment_times),
        total_prs=len(ticket["prs"]),
        latest_at=max(deployment_times, default=None),
    )


def _check_name(check: dict) -> str:
    name = check.get("name") or check.get("context") or "unnamed check"
    workflow = check.get("workflowName")
    return f"{workflow} / {name}" if workflow else name


def _check_outcome(check: dict) -> str:
    return check.get("conclusion") or check.get("state") or check.get("status") or "UNKNOWN"


def _format_check(check: dict) -> str:
    outcome = _check_outcome(check)
    if outcome == "SUCCESS":
        marker, colour = "✓", GREEN
    elif outcome in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"}:
        marker, colour = "×", RED
    else:
        marker, colour = "•", YELLOW
    return f"  {colour}{marker}{RESET} {_check_name(check)} {DIM}· {outcome.lower()}{RESET}"


def _format_pr_signal(pr: dict) -> str:
    check_count = len(pr["checks"])
    passing_count = sum(_check_outcome(check) == "SUCCESS" for check in pr["checks"])
    checks = f"CI {passing_count}/{check_count} passing" if check_count else "no CI checks reported"
    prod = "in production" if pr["in_prod"] else "not in production"
    signals = [pr["state"].lower()]
    if pr["state"] == "OPEN":
        signals.append("ready label present" if pr["ready_for_review"] else "ready label absent")
    signals.extend([checks, prod])
    return " · ".join(signals)


def format_pr(pr: dict) -> str:
    if pr["state"] == "ERROR":
        return "\n".join(
            [
                f"{BOLD}{pr['owner']}/{pr['repo']}#{pr['number']}{RESET}",
                f"  State       {RED}lookup error{RESET}",
                f"  Error       {pr['error']}",
            ],
        )
    lines = [
        f"{BOLD}{pr['owner']}/{pr['repo']}#{pr['number']} — {pr['title']}{RESET}",
        f"  {_format_pr_signal(pr)}",
        f"  Labels: {', '.join(pr['labels']) or 'none'}",
        f"  Merge {_short_sha(pr['merge_sha'])} · production {_short_sha(pr['prod_sha'])}",
        "",
        f"{BOLD}Checks{RESET}",
    ]
    if not pr["checks"]:
        lines.append(f"  {DIM}none reported{RESET}")
    lines.extend(_format_check(check) for check in pr["checks"])
    lines.extend(["", f"{DIM}{_terminal_link('Open pull request', pr['url'])}{RESET}"])
    return "\n".join(lines)


def format_production_evidence(ticket: dict) -> str:
    lines = [f"{BOLD}{GREEN}Production evidence for Released{RESET}"]
    confirmation = get_ticket_production_confirmation(ticket)
    if confirmation.latest_at and confirmation.confirmed_prs == confirmation.total_prs:
        lines.append(f"  {GREEN}✓ All linked PRs confirmed in production by {_format_deployed_at(confirmation.latest_at)}{RESET}")
    elif confirmation.latest_at:
        lines.append(
            f"  {YELLOW}• Production confirmed for {confirmation.confirmed_prs}/{confirmation.total_prs} linked PRs "
            f"by {_format_deployed_at(confirmation.latest_at)}{RESET}",
        )
    for pr in ticket["prs"]:
        if not pr["in_prod"]:
            lines.append(f"  {YELLOW}• {pr['owner']}/{pr['repo']}#{pr['number']} — no matching production deployment found{RESET}")
            continue
        lines.extend(
            [
                f"  {pr['owner']}/{pr['repo']}#{pr['number']} merge {_short_sha(pr['merge_sha'])}",
                f"    matched production deployment · {_format_deployed_at(pr['prod_deployed_at'])}",
                f"    included in deployment {_short_sha(pr['prod_sha'])}",
            ],
        )
        if pr["prod_deployment_url"]:
            lines.append(f"    {DIM}{_terminal_link('Open production deployment', pr['prod_deployment_url'])}{RESET}")
    return "\n".join(lines)


def format_ticket(ticket: dict) -> str:
    lines = [
        f"{BOLD}{CYAN}{ticket['men']} — {ticket['title']}{RESET}",
        f"{ticket['current']} → {GREEN}{ticket['proposed']}{RESET}",
    ]
    if ticket["proposed"] == "Released":
        lines.extend(["", format_production_evidence(ticket)])
    if not ticket["prs"]:
        lines.extend(["", "No linked pull requests."])
    for pr in ticket["prs"]:
        lines.extend(["", format_pr(pr)])
    lines.extend(["", f"{DIM}{_terminal_link('Open Notion ticket', ticket['url'])}{RESET}"])
    return "\n".join(lines)


def format_status_candidate(ticket: dict) -> str:
    status_label = ticket["proposed"]
    if status_label == "Released":
        confirmation = get_ticket_production_confirmation(ticket)
        if confirmation.latest_at and confirmation.confirmed_prs == confirmation.total_prs:
            status_label = f"Released · prod confirmed by {_format_deployed_at(confirmation.latest_at)}"
        elif confirmation.latest_at:
            status_label = (
                f"Released · prod evidence {confirmation.confirmed_prs}/{confirmation.total_prs} "
                f"by {_format_deployed_at(confirmation.latest_at)}"
            )
    return f"{GREEN}[{status_label}]{RESET} {CYAN}{ticket['men']}{RESET} · {ticket['title']}"


def format_ready_pr(candidate: dict) -> str:
    ticket_lines = [
        f"  {ticket['men']} — {ticket['title']} ({ticket['current']} → {ticket['proposed'] or 'no change'})"
        for ticket in candidate["tickets"]
    ]
    return "\n".join(
        [
            format_pr(candidate["pr"]),
            "",
            f"{BOLD}Linked tickets{RESET}",
            *ticket_lines,
            "",
            f"{DIM}Applying Ready for Review may cause these tickets to propose In Review.{RESET}",
        ],
    )


CANDIDATE_FORMATTERS = {"ready": format_ready_pr, "status": format_ticket}


def select_candidates(candidates: list[dict], *, mode: str) -> list[dict]:
    if not candidates:
        return []
    formatter = CANDIDATE_FORMATTERS[mode]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as snapshot:
        json.dump({"mode": mode, "candidates": candidates}, snapshot, default=_datetime_json_default)
        snapshot.flush()
        preview_command = shlex.join([sys.executable, "-m", "shipshape.tickets.cli", "preview", snapshot.name]) + " {1}"
        if mode == "ready":
            rows = [
                f"{index}\t{YELLOW}[Ready for Review]{RESET} "
                f"{candidate['pr']['owner']}/{candidate['pr']['repo']}#{candidate['pr']['number']} · {candidate['pr']['title']}"
                for index, candidate in enumerate(candidates)
            ]
            prompt = "mark-ready> "
        else:
            rows = [f"{index}\t{format_status_candidate(candidate)}" for index, candidate in enumerate(candidates)]
            prompt = "tickets> "
        selected_rows = run_fzf(
            rows,
            prompt=prompt,
            header=f"{len(candidates)} proposed  |  SPACE select + next · CTRL-A all · CTRL-D clear · ENTER review",
            preview=preview_command,
            preview_height=get_preview_height(formatter(candidate) for candidate in candidates),
        ).rows
    return [candidates[int(row.split("\t", 1)[0])] for row in selected_rows]


def preview(snapshot_path: str, candidate_index: str) -> None:
    snapshot = json.loads(Path(snapshot_path).read_text())
    candidate = snapshot["candidates"][int(candidate_index)]
    if snapshot["mode"] == "status":
        for pr in candidate["prs"]:
            if pr.get("prod_deployed_at"):
                pr["prod_deployed_at"] = datetime.fromisoformat(pr["prod_deployed_at"])
    print(CANDIDATE_FORMATTERS[snapshot["mode"]](candidate))


def print_context(candidates: list[dict], *, mode: str) -> None:
    formatter = CANDIDATE_FORMATTERS[mode]
    print()
    for index, candidate in enumerate(candidates):
        if index:
            print(f"\n{DIM}{'─' * 80}{RESET}\n")
        print(formatter(candidate))
    print()


def offer_ready_labels(tickets: list[dict]) -> list[dict]:
    candidates = get_ready_pr_candidates(tickets)
    if not candidates:
        return tickets
    selected = select_candidates(candidates, mode="ready")
    if not selected:
        return tickets
    print_context(selected, mode="ready")
    if not confirm(f"mark {len(selected)} pull request(s) Ready for Review"):
        print(f"{DIM}Skipped Ready for Review labels.{RESET}")
        return tickets
    for candidate in selected:
        pr = candidate["pr"]
        engine.add_ready_label(pr["owner"], pr["repo"], pr["number"])
    return engine.collect_tickets()


def offer_status_updates(tickets: list[dict]) -> int:
    candidates = get_status_candidates(tickets)
    if not candidates:
        print(f"{GREEN}Ticket statuses already match their PR/deployment evidence.{RESET}")
        return 0
    selected = select_candidates(candidates, mode="status")
    if not selected:
        print(f"{DIM}No ticket statuses selected. Nothing changed.{RESET}")
        return 0
    print_context(selected, mode="status")
    if not confirm(f"apply {len(selected)} Notion status change(s)"):
        print(f"{DIM}Cancelled. No ticket statuses changed.{RESET}")
        return 0
    for ticket in selected:
        engine.set_status(ticket["page_id"], ticket["proposed"])
        print(f"{GREEN}OK{RESET} {ticket['men']} → {ticket['proposed']}")
    print(f"Applied {len(selected)}.")
    return 0


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "preview":
        preview(sys.argv[2], sys.argv[3])
        return 0
    if "--setup" in sys.argv:
        emails = [arg for arg in sys.argv[1:] if arg != "--setup"]
        engine.resolve_and_store_user_id(emails[0] if emails else None)
        return 0
    require("gh", "fzf", "gum", gh_auth=True)

    print(f"{DIM}Reading your Notion tickets, linked PRs, checks, and production deployments…{RESET}", end="", flush=True)
    tickets = engine.collect_tickets()
    print("\r\033[2K", end="", flush=True)
    tickets = offer_ready_labels(tickets)
    return offer_status_updates(tickets)


if __name__ == "__main__":
    raise SystemExit(main())
