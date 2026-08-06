from __future__ import annotations

import unittest
from unittest.mock import patch

from shipshape.tickets import engine
from shipshape.tickets.cli import offer_ready_labels


class TicketCollectionTests(unittest.TestCase):
    def test_collect_tickets_fetches_each_pr_and_repository_once(self) -> None:
        first_ref = engine.PRRef("modoenergy", "ask-modo", 1)
        second_ref = engine.PRRef("modoenergy", "ask-modo", 2)
        parsed_tickets = [
            {
                "page_id": "page-1",
                "url": "https://notion.so/page-1",
                "men": "MEN-1",
                "title": "First",
                "status": "In Progress",
                "pr_refs": [first_ref],
            },
            {
                "page_id": "page-2",
                "url": "https://notion.so/page-2",
                "men": "MEN-2",
                "title": "Second",
                "status": "In Progress",
                "pr_refs": [first_ref, second_ref],
            },
        ]

        def pr_state(ref: engine.PRRef) -> dict:
            return {
                "owner": ref.owner,
                "repo": ref.repo,
                "number": ref.number,
                "state": "OPEN",
                "ready_for_review": False,
            }

        with (
            patch("shipshape.tickets.engine.notion_user_id", return_value="user"),
            patch("shipshape.tickets.engine.query_my_tickets", return_value=[{}, {}]),
            patch("shipshape.tickets.engine.parse_ticket", side_effect=parsed_tickets),
            patch(
                "shipshape.tickets.engine.get_prod_deployments", return_value=[]
            ) as get_deployments,
            patch(
                "shipshape.tickets.engine.gh_pr_state", side_effect=pr_state
            ) as get_pr,
        ):
            tickets = engine.collect_tickets()

        get_deployments.assert_called_once_with("modoenergy", "ask-modo")
        self.assertEqual(get_pr.call_count, 2)
        self.assertCountEqual(
            (call.args[0] for call in get_pr.call_args_list), [first_ref, second_ref]
        )
        self.assertEqual([len(ticket["prs"]) for ticket in tickets], [1, 2])


class ReadyLabelTests(unittest.TestCase):
    def test_offer_ready_labels_updates_collected_tickets_without_refetching(
        self,
    ) -> None:
        pr = {
            "owner": "modoenergy",
            "repo": "ask-modo",
            "number": 1,
            "title": "Example",
            "url": "https://github.com/modoenergy/ask-modo/pull/1",
            "state": "OPEN",
            "labels": [],
            "ready_for_review": False,
            "checks_passing": True,
            "checks": [],
            "in_prod": False,
            "merge_sha": None,
            "prod_sha": None,
        }
        ticket = {
            "men": "MEN-1",
            "title": "Example ticket",
            "current": "In Progress",
            "proposed": None,
            "prs": [pr],
        }
        tickets = [ticket]

        with (
            patch(
                "shipshape.tickets.cli.select_candidates",
                side_effect=lambda candidates, **_: candidates,
            ),
            patch("shipshape.tickets.cli.print_context"),
            patch("shipshape.tickets.cli.confirm", return_value=True),
            patch("shipshape.tickets.cli.engine.add_ready_label") as add_ready_label,
            patch("shipshape.tickets.cli.engine.collect_tickets") as collect_tickets,
        ):
            returned = offer_ready_labels(tickets)

        add_ready_label.assert_called_once_with("modoenergy", "ask-modo", 1)
        collect_tickets.assert_not_called()
        self.assertIs(returned, tickets)
        self.assertTrue(pr["ready_for_review"])
        self.assertEqual(pr["labels"], [engine.READY_FOR_REVIEW_LABEL])
        self.assertEqual(ticket["proposed"], "In Review")


if __name__ == "__main__":
    unittest.main()
